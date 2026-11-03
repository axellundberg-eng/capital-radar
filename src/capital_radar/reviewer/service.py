"""Granskartjänst: förbereder indata, gör högst ett provideranrop per jobb
och lämnar utlåtanden till lagringen.

Granskaren pratar med databasen bara via ``ReviewStore`` — i drift
``HttpReviewStore`` mot Capital Radar-API:t. Den skriver ALDRIG i DeepSeeks
tabeller och startar ingen extraction.

Felklassificering (samma princip som extraction_v2):
    schemafel/okända id → validation_failed, timeout → timeout,
    rate limit → rate_limited, max_tokens → validation_failed,
    vägran → refused, övrigt providerfel → failed.
Saknas utlåtanden för vissa rader sparas resten med status incomplete.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from ..hashing import compute_idempotency_key, compute_input_hash
from . import prompts
from .inputs import normalize_for_match, render_extraction_prompt, render_headline_prompt
from .models import ExtractionReview, HeadlineBatchReview
from .providers import (
    MODEL_PRICES_USD_PER_MTOK,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ReviewProvider,
    build_provider,
)
from .store import HttpReviewStore, ReviewStore, ReviewStoreError, urllib_transport

# Grovt estimat för svensk text; dry-run gör inga API-anrop mot modellen
# (inte ens count_tokens). Erfarenhetsmässigt ~3,5 tecken per token.
CHARS_PER_TOKEN = 3.5
EXPECTED_OUTPUT_TOKENS = {prompts.EXTRACTION_TASK: 1500, prompts.HEADLINE_TASK: 1500}


def estimate_tokens(*texts: str) -> int:
    return math.ceil(sum(len(t) for t in texts) / CHARS_PER_TOKEN)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = MODEL_PRICES_USD_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


@dataclass
class PreparedReview:
    task: str
    prompt_version: str
    system: str
    user: str
    response_model: type[BaseModel]
    targets: set[tuple[str, int]]
    issuer_id: Optional[int] = None
    document_id: Optional[int] = None
    extraction_job_id: Optional[int] = None
    quote_haystack: str = ""
    titles: dict[int, str] = field(default_factory=dict)
    input_hash: str = ""
    idempotency_key: str = ""
    estimated_input_tokens: int = 0


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return transform_schema(model)


def _finalize(prepared: PreparedReview, *, model: str, case_id: str) -> PreparedReview:
    prepared.input_hash = compute_input_hash(
        agent_type=prepared.task,
        prompt_version=prepared.prompt_version,
        system_prompt=prepared.system,
        user_prompt=prepared.user,
        model=model,
    )
    prepared.idempotency_key = compute_idempotency_key(
        agent_type=prepared.task,
        issuer_id=prepared.issuer_id,
        case_id=case_id,
        input_hash=prepared.input_hash,
    )
    schema_text = json.dumps(_schema_for(prepared.response_model))
    prepared.estimated_input_tokens = estimate_tokens(
        prepared.system, prepared.user, schema_text
    )
    return prepared


class ReviewService:
    def __init__(
        self,
        store: ReviewStore,
        settings: Any,
        *,
        provider: ReviewProvider | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.provider = provider
        self.model = provider.model if provider is not None else settings.reviewer_model
        self.provider_name = provider.name if provider is not None else "anthropic"

    # ------------------------------------------------------------------ prep

    def prepare_extraction_reviews(
        self, *, limit: int, retry_failed: bool = False
    ) -> list[PreparedReview]:
        items = self.store.list_extraction_inputs(
            task=prompts.EXTRACTION_TASK,
            model=self.model,
            prompt_version=prompts.EXTRACTION_PROMPT_VERSION,
            limit=limit,
            retry_failed=retry_failed,
            max_source_chars=self.settings.reviewer_max_source_chars,
        )
        return [
            _finalize(
                PreparedReview(
                    task=prompts.EXTRACTION_TASK,
                    prompt_version=prompts.EXTRACTION_PROMPT_VERSION,
                    system=prompts.EXTRACTION_SYSTEM_PROMPT,
                    user=render_extraction_prompt(item),
                    response_model=ExtractionReview,
                    targets=item.targets,
                    issuer_id=item.issuer_id,
                    document_id=item.document_id,
                    extraction_job_id=item.extraction_job_id,
                    quote_haystack=item.quote_haystack,
                ),
                model=self.model,
                case_id=f"extraction_job:{item.extraction_job_id}",
            )
            for item in items
            if item.targets
        ]

    def prepare_headline_reviews(self, *, limit: int) -> list[PreparedReview]:
        candidates = self.store.list_headline_candidates(
            task=prompts.HEADLINE_TASK,
            model=self.model,
            prompt_version=prompts.HEADLINE_PROMPT_VERSION,
            limit=limit,
        )
        size = self.settings.reviewer_headline_batch_size
        prepared: list[PreparedReview] = []
        for start in range(0, len(candidates), size):
            batch = candidates[start:start + size]
            prepared.append(
                _finalize(
                    PreparedReview(
                        task=prompts.HEADLINE_TASK,
                        prompt_version=prompts.HEADLINE_PROMPT_VERSION,
                        system=prompts.HEADLINE_SYSTEM_PROMPT,
                        user=render_headline_prompt(batch),
                        response_model=HeadlineBatchReview,
                        targets={("document", c.document_id) for c in batch},
                        titles={c.document_id: c.title for c in batch},
                    ),
                    model=self.model,
                    case_id="headline_batch:" + ",".join(
                        str(c.document_id) for c in batch
                    ),
                )
            )
        return prepared

    # ------------------------------------------------------------------- run

    def run(
        self,
        task: str,
        *,
        limit: int,
        dry_run: bool = False,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        if not dry_run and self.provider is None:
            return {
                **self._summary(task, []),
                "status": "disabled",
                "reason": "ANTHROPIC_API_KEY saknas — granskaren är avstängd",
                "results": [],
            }
        if task == prompts.EXTRACTION_TASK:
            prepared = self.prepare_extraction_reviews(limit=limit, retry_failed=retry_failed)
        elif task == prompts.HEADLINE_TASK:
            prepared = self.prepare_headline_reviews(limit=limit)
        else:
            raise ValueError(f"Okänd granskningsuppgift: {task}")
        summary = self._summary(task, prepared)
        if dry_run:
            return {**summary, "status": "dry_run", "prepared": prepared}
        results = asyncio.run(self._run_all(prepared, retry_failed=retry_failed))
        counts: dict[str, int] = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        return {**summary, "status": "done", "counts": counts, "results": results}

    def _summary(self, task: str, prepared: list[PreparedReview]) -> dict[str, Any]:
        input_tokens = sum(p.estimated_input_tokens for p in prepared)
        output_tokens = EXPECTED_OUTPUT_TOKENS[task] * len(prepared)
        return {
            "task": task,
            "model": self.model,
            "jobs": len(prepared),
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_cost_usd": estimate_cost_usd(self.model, input_tokens, output_tokens),
        }

    async def _run_all(
        self, prepared: list[PreparedReview], *, retry_failed: bool
    ) -> list[dict[str, Any]]:
        try:
            # Sekventiellt: granskaren ska vara billig och aldrig tränga
            # undan extraction-flödets provider-kapacitet.
            return [
                await self.execute(item, retry_failed=retry_failed)
                for item in prepared
            ]
        finally:
            await self.provider.aclose()

    # --------------------------------------------------------------- execute

    async def execute(
        self, prepared: PreparedReview, *, retry_failed: bool = False
    ) -> dict[str, Any]:
        """Ett granskningsjobb = högst ett provideranrop."""
        if self.provider is None:
            return {"status": "disabled", "idempotency_key": prepared.idempotency_key}
        run_id = self.store.claim({
            "task": prepared.task,
            "provider": self.provider_name,
            "model": self.model,
            "prompt_version": prepared.prompt_version,
            "input_sha256": prepared.input_hash,
            "idempotency_key": prepared.idempotency_key,
            "issuer_id": prepared.issuer_id,
            "document_id": prepared.document_id,
            "extraction_job_id": prepared.extraction_job_id,
            "estimated_input_tokens": prepared.estimated_input_tokens,
            "hard_timeout_seconds": self.settings.reviewer_hard_timeout_seconds,
            "retry_failed": retry_failed,
        })
        if run_id is None:
            return {"status": "skipped_duplicate", "idempotency_key": prepared.idempotency_key}

        start = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.reviewer_hard_timeout_seconds):
                result = await self.provider.complete(
                    system=prepared.system,
                    user=prepared.user,
                    schema=_schema_for(prepared.response_model),
                    max_tokens=self.settings.reviewer_max_output_tokens,
                )
        except (TimeoutError, ProviderTimeout) as exc:
            return self._finish(run_id, {}, "timeout", "timeout", str(exc) or "hård timeout")
        except ProviderRateLimited as exc:
            return self._finish(run_id, {}, "rate_limited", exc.error_type, str(exc))
        except ProviderError as exc:
            return self._finish(run_id, {}, "failed", exc.error_type, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._finish(run_id, {}, "failed", "unexpected", str(exc))
        base = {
            "response_id": result.response_id,
            "stop_reason": result.stop_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "raw_output": result.text,
        }
        if result.stop_reason == "refusal":
            return self._finish(run_id, base, "refused", "refusal", "providern vägrade")
        if result.stop_reason == "max_tokens":
            return self._finish(run_id, base, "validation_failed", "max_tokens", "svaret avkortades")
        try:
            verdicts, missing = self._validate(prepared, result.text)
        except (ValidationError, ValueError) as exc:
            return self._finish(run_id, base, "validation_failed", "schema_validation", str(exc))
        status = "incomplete" if missing else "completed"
        error = (
            "utlåtande saknas för " + ", ".join(f"{k} {i}" for k, i in sorted(missing))
            if missing else None
        )
        outcome = self._finish(run_id, {**base, "verdicts": verdicts}, status, None, error)
        counts: dict[str, int] = {}
        for verdict in verdicts:
            counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1
        return {**outcome, "verdicts": counts}

    def _finish(
        self,
        run_id: int,
        base: dict[str, Any],
        status: str,
        error_type: str | None,
        error: str | None,
    ) -> dict[str, Any]:
        payload = {"status": status, "error_type": error_type, "error": error, **base}
        try:
            self.store.finish(run_id, payload)
        except ReviewStoreError as exc:
            # Svaret är betalt men kunde inte sparas; run står kvar som
            # running tills deadline och kan då tas om med --retry-failed.
            return {"status": "store_failed", "run_id": run_id, "error": str(exc)}
        return {
            "status": status, "run_id": run_id, "error_type": error_type,
            "input_tokens": base.get("input_tokens"),
            "output_tokens": base.get("output_tokens"),
        }

    def _validate(
        self, prepared: PreparedReview, text: str | None
    ) -> tuple[list[dict[str, Any]], set[tuple[str, int]]]:
        """Returnerar (verdict-rader, saknade mål). Kastar ValueError vid schemafel."""
        if not text:
            raise ValueError("tomt svar")
        parsed = prepared.response_model.model_validate_json(text)
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        if isinstance(parsed, ExtractionReview):
            for item in parsed.items:
                self._check_target(prepared, (item.target_kind, item.target_id), seen)
                rows.append({
                    "target_kind": item.target_kind,
                    "target_id": item.target_id,
                    "verdict": item.verdict.value,
                    "issues": [issue.value for issue in item.issues],
                    "reasoning": item.reasoning,
                    "source_quote": item.source_quote,
                    "quote_found": (
                        None if not item.source_quote.strip()
                        else int(normalize_for_match(item.source_quote) in prepared.quote_haystack)
                    ),
                    "expected_value": item.expected_value,
                })
        else:
            assert isinstance(parsed, HeadlineBatchReview)
            for flag in parsed.documents:
                self._check_target(prepared, ("document", flag.document_id), seen)
                rows.append({
                    "target_kind": "document",
                    "target_id": flag.document_id,
                    "verdict": "flag" if flag.may_affect_cash else "no_flag",
                    "issues": [],
                    "reasoning": flag.reasoning,
                    "source_quote": prepared.titles.get(flag.document_id, ""),
                    "quote_found": None,
                    "expected_value": None,
                })
        return rows, prepared.targets - seen

    @staticmethod
    def _check_target(
        prepared: PreparedReview, key: tuple[str, int], seen: set[tuple[str, int]]
    ) -> None:
        if key not in prepared.targets:
            raise ValueError(f"okänt mål i svaret: {key[0]} {key[1]}")
        if key in seen:
            raise ValueError(f"dubbelt utlåtande för {key[0]} {key[1]}")
        seen.add(key)


def build_review_service(settings: Any, *, store: ReviewStore | None = None) -> ReviewService:
    """Granskare som pratar med Capital Radar-API:t (REVIEWER_API_BASE_URL)."""
    if store is None:
        store = HttpReviewStore(
            urllib_transport(
                settings.reviewer_api_base_url,
                settings.review_api_token or "",
                settings.reviewer_api_timeout_seconds,
            )
        )
    return ReviewService(store, settings, provider=build_provider(settings))
