"""Agent execution wrapper med idempotency, usage recording och concurrency pools.

AgentRunner kör ett agent-anrop via en :class:`AIProvider` och spårar varje
körning i ``agent_runs`` med execution profile, token-användning och kostnad.
Resultat sparas som versionerade ``agent_artifacts``.

Designregler:
- Samma ``idempotency_key`` återanvänder befintligt run (``skipped_idempotent``).
- Samma content hash återanvänder befintligt artifact (ingen ny version).
- Misslyckat run skriver aldrig över ett tidigare completed-artifact.
- Fullständiga prompts, API-nycklar och chain-of-thought lagras aldrig.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .ai.base import AIProvider
from .ai.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from .ai.health import ProviderHealthRegistry
from .ai.response import ProviderResponse
from .ai.router import ExecutionProfile
from .db import Database
from .normalization import utc_now

logger = logging.getLogger("bergs.ai.agent_runner")

# Separata concurrency pools — ett stort batchjobb får inte blockera Chat.
POOL_LIMITS: dict[str, int] = {
    "background_zero_token": 8,
    "background_flash": 4,
    "analysis_pro": 2,
    "critical_pro": 1,
    "interactive_chat": 2,
}

_pool_semaphores: dict[str, threading.BoundedSemaphore] = {}
_pool_lock = threading.Lock()


def pool_semaphore(pool: str, *, limit: Optional[int] = None) -> threading.BoundedSemaphore:
    """Returnera (eller skapa) semaphore för en concurrency pool."""
    with _pool_lock:
        semaphore = _pool_semaphores.get(pool)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit or POOL_LIMITS.get(pool, 2))
            _pool_semaphores[pool] = semaphore
        return semaphore


def reset_pools() -> None:
    """Återställ pools — endast för tester."""
    with _pool_lock:
        _pool_semaphores.clear()


def compute_input_hash(
    *,
    agent_type: str,
    prompt_version: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """Stabil hash över allt som styr ett agent-anrop."""
    digest = hashlib.sha256()
    for part in (agent_type, prompt_version, model, system_prompt, user_prompt):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def compute_idempotency_key(
    *,
    agent_type: str,
    issuer_id: Optional[int],
    case_id: Optional[str],
    input_hash: str,
) -> str:
    """Nyckel som gör ett run idempotent för samma input och scope."""
    raw = f"{agent_type}|{issuer_id or ''}|{case_id or ''}|{input_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentRunOutcome:
    """Resultat av ett AgentRunner-anrop."""

    run_id: int
    status: str  # completed | failed | skipped_idempotent | unavailable
    artifact_id: Optional[int]
    response: Optional[ProviderResponse]
    error_type: Optional[str] = None
    safe_error_message: Optional[str] = None
    latency_ms: Optional[int] = None
    retry_count: int = 0


class AgentRunner:
    """Kör AI-agenter med spårning, retries och idempotency."""

    def __init__(
        self,
        db: Database,
        *,
        cost_per_1k_input: Mapping[str, float] | None = None,
        cost_per_1k_output: Mapping[str, float] | None = None,
        cost_tracking_enabled: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        health_registry: ProviderHealthRegistry | None = None,
        settings: Any | None = None,
    ) -> None:
        self._db = db
        # Priser ligger i injicerad config — aldrig hårdkodade i analyslogik.
        self._cost_input = dict(cost_per_1k_input or {})
        self._cost_output = dict(cost_per_1k_output or {})
        self._cost_tracking = cost_tracking_enabled
        self._sleep = sleep
        self._health = health_registry
        self._settings = settings

    # ------------------------------------------------------------------ API

    def run(
        self,
        *,
        agent_type: str,
        provider: Optional[AIProvider],
        profile: ExecutionProfile,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, Any],
        prompt_version: str,
        issuer_id: Optional[int] = None,
        case_id: Optional[str] = None,
        deal_id: Optional[int] = None,
        source_data_cutoff: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        validate: Optional[Callable[[Any], tuple[bool, Optional[float], list]]] = None,
        unavailable_reason: Optional[str] = None,
    ) -> AgentRunOutcome:
        """Kör ett agent-anrop end-to-end.

        ``validate`` får parsad JSON och returnerar
        ``(ok, confidence, evidence_list)``. Vid ``ok=False`` markeras run
        som failed och inget artifact sparas.
        """
        input_hash = compute_input_hash(
            agent_type=agent_type,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=profile.model,
        )
        idem_key = compute_idempotency_key(
            agent_type=agent_type,
            issuer_id=issuer_id,
            case_id=case_id,
            input_hash=input_hash,
        )

        existing = self._find_run(idem_key)
        if existing is not None and existing["status"] == "completed":
            return AgentRunOutcome(
                run_id=int(existing["id"]),
                status="skipped_idempotent",
                artifact_id=existing["artifact_id"],
                response=None,
                latency_ms=existing["latency_ms"],
            )

        if provider is None:
            run_id = self._record_run(
                idem_key=idem_key,
                agent_type=agent_type,
                profile=profile,
                prompt_version=prompt_version,
                input_hash=input_hash,
                issuer_id=issuer_id,
                case_id=case_id,
                deal_id=deal_id,
                source_data_cutoff=source_data_cutoff,
                status="unavailable",
                error_type="provider_unavailable",
                safe_error_message=unavailable_reason or "provider ej konfigurerad",
            )
            return AgentRunOutcome(
                run_id=run_id,
                status="unavailable",
                artifact_id=None,
                response=None,
                error_type="provider_unavailable",
                safe_error_message=unavailable_reason or "provider ej konfigurerad",
            )

        provider_key = f"{profile.provider}/{profile.model}"
        if self._health is not None and not self._health.allow_request(
            provider_key, priority=profile.priority
        ):
            # Circuit open — blockera lågprioritetsanrop temporärt.
            # Sparade analyser påverkas aldrig; run markeras unavailable.
            run_id = self._record_run(
                idem_key=idem_key,
                agent_type=agent_type,
                profile=profile,
                prompt_version=prompt_version,
                input_hash=input_hash,
                issuer_id=issuer_id,
                case_id=case_id,
                deal_id=deal_id,
                source_data_cutoff=source_data_cutoff,
                status="unavailable",
                error_type="circuit_open",
                safe_error_message="provider circuit breaker open — anrop skjuts upp",
            )
            return AgentRunOutcome(
                run_id=run_id,
                status="unavailable",
                artifact_id=None,
                response=None,
                error_type="circuit_open",
                safe_error_message="provider circuit breaker open — anrop skjuts upp",
            )

        # Egress-spärr (P1): icke-publik data får aldrig lämna systemet när
        # ``private_data_external_egress_enabled`` är avstängd. Spärren sitter
        # före all nätverksaktivitet och loggar aldrig prompt- eller
        # innehållsdata — bara klassifikationen. DB-status är "blocked"
        # (policy-denied före provider-anrop; schema v7) medan
        # error_type="egress_blocked" skiljer spärren från circuit-open.
        if self._egress_blocked(profile):
            safe_message = (
                "extern egress blockerad för dataklassifikation "
                f"'{profile.data_classification}'"
            )
            run_id = self._record_run(
                idem_key=idem_key,
                agent_type=agent_type,
                profile=profile,
                prompt_version=prompt_version,
                input_hash=input_hash,
                issuer_id=issuer_id,
                case_id=case_id,
                deal_id=deal_id,
                source_data_cutoff=source_data_cutoff,
                status="blocked",
                error_type="egress_blocked",
                safe_error_message=safe_message,
            )
            logger.warning(
                "egress_blocked: agent_type=%s classification=%s run_id=%s",
                agent_type,
                profile.data_classification,
                run_id,
            )
            return AgentRunOutcome(
                run_id=run_id,
                status="blocked",
                artifact_id=None,
                response=None,
                error_type="egress_blocked",
                safe_error_message=safe_message,
            )

        run_id = self._record_run(
            idem_key=idem_key,
            agent_type=agent_type,
            profile=profile,
            prompt_version=prompt_version,
            input_hash=input_hash,
            issuer_id=issuer_id,
            case_id=case_id,
            deal_id=deal_id,
            source_data_cutoff=source_data_cutoff,
            status="running",
        )

        semaphore = pool_semaphore(profile.concurrency_pool)
        retry_policy = profile.retry_policy
        attempt = 0
        last_error: Optional[ProviderError] = None
        started = time.monotonic()
        with semaphore:
            while attempt < max(1, retry_policy.max_attempts):
                attempt += 1
                try:
                    response = provider.structured_completion(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        schema=schema,
                        schema_name=profile.response_schema or agent_type,
                        prompt_version=prompt_version,
                        max_output_tokens=profile.max_output_tokens or None,
                        thinking="enabled" if profile.thinking_enabled else "disabled",
                        reasoning_effort=profile.reasoning_effort,
                        prompt_cache_key=f"{agent_type}-{prompt_version}",
                        request_metadata={
                            "agent_type": agent_type,
                            "prompt_version": prompt_version,
                            "profile_version": profile.profile_version,
                        },
                    )
                    last_error = None
                    break
                except (ProviderRateLimitError, ProviderServerError) as exc:
                    last_error = exc
                    if attempt < retry_policy.max_attempts:
                        self._sleep(retry_policy.backoff_base_seconds * attempt)
                except ProviderTimeoutError as exc:
                    last_error = exc
                    if retry_policy.retry_on_timeout and attempt < retry_policy.max_attempts:
                        self._sleep(retry_policy.backoff_base_seconds * attempt)
                    else:
                        break
                except ProviderError as exc:
                    # Schema-/request-fel ska aldrig retrias blint.
                    last_error = exc
                    break
            else:  # pragma: no cover - loop always breaks
                pass

        latency_ms = int((time.monotonic() - started) * 1000)
        retry_count = attempt - 1

        if self._health is not None:
            if last_error is not None:
                self._health.record_failure(provider_key, last_error)
            else:
                self._health.record_success(provider_key, latency_ms=latency_ms)

        if last_error is not None:
            self._finish_run(
                run_id,
                status="failed",
                latency_ms=latency_ms,
                retry_count=retry_count,
                error_type=type(last_error).__name__,
                safe_error_message=str(last_error)[:300],
            )
            return AgentRunOutcome(
                run_id=run_id,
                status="failed",
                artifact_id=None,
                response=None,
                error_type=type(last_error).__name__,
                safe_error_message=str(last_error)[:300],
                latency_ms=latency_ms,
                retry_count=retry_count,
            )

        assert response is not None  # noqa: S101 - loop guarantees
        parsed, parse_error = self._parse_json(response.content)
        confidence: Optional[float] = None
        evidence: list = []
        if parse_error is not None:
            self._finish_run(
                run_id,
                status="failed",
                latency_ms=latency_ms,
                retry_count=retry_count,
                response=response,
                error_type="invalid_output",
                safe_error_message=parse_error,
            )
            return AgentRunOutcome(
                run_id=run_id,
                status="failed",
                artifact_id=None,
                response=response,
                error_type="invalid_output",
                safe_error_message=parse_error,
                latency_ms=latency_ms,
                retry_count=retry_count,
            )

        if validate is not None:
            ok, confidence, evidence = validate(parsed)
            if not ok:
                self._finish_run(
                    run_id,
                    status="failed",
                    latency_ms=latency_ms,
                    retry_count=retry_count,
                    response=response,
                    error_type="schema_validation_failed",
                    safe_error_message="modelloutput klarade inte lokal schemavalidering",
                )
                return AgentRunOutcome(
                    run_id=run_id,
                    status="failed",
                    artifact_id=None,
                    response=response,
                    error_type="schema_validation_failed",
                    safe_error_message="modelloutput klarade inte lokal schemavalidering",
                    latency_ms=latency_ms,
                    retry_count=retry_count,
                )
        elif isinstance(parsed, Mapping):
            raw_conf = parsed.get("confidence")
            if isinstance(raw_conf, (int, float)):
                confidence = float(raw_conf)

        artifact_id = self._store_artifact(
            agent_type=agent_type,
            artifact_kind=artifact_kind or agent_type,
            issuer_id=issuer_id,
            case_id=case_id,
            content=parsed,
            confidence=confidence,
            evidence=evidence,
            source_data_cutoff=source_data_cutoff,
        )
        self._finish_run(
            run_id,
            status="completed",
            latency_ms=latency_ms,
            retry_count=retry_count,
            response=response,
            artifact_id=artifact_id,
            confidence=confidence,
        )
        return AgentRunOutcome(
            run_id=run_id,
            status="completed",
            artifact_id=artifact_id,
            response=response,
            latency_ms=latency_ms,
            retry_count=retry_count,
        )

    # -------------------------------------------------------------- interna

    def _egress_blocked(self, profile: ExecutionProfile) -> bool:
        """Returnera True om profilens dataklassifikation inte får lämna systemet.

        Publik data är alltid tillåten. Allt annat (private, potential_mnpi,
        confirmed_mnpi, …) blockeras när
        ``settings.private_data_external_egress_enabled`` är falskt. Saknade
        settings tolkas som avstängd (säker default).
        """
        if profile.data_classification == "public":
            return False
        return not bool(
            getattr(self._settings, "private_data_external_egress_enabled", False)
        )

    @staticmethod
    def _parse_json(content: str) -> tuple[Any, Optional[str]]:
        text = (content or "").strip()
        if not text:
            return None, "tomt modellsvar"
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            # Tolerant parsing: försök extrahera första JSON-objektet.
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1]), None
                except json.JSONDecodeError:
                    pass
            return None, "modellsvar är inte giltig JSON"

    def _find_run(self, idem_key: str) -> Optional[sqlite3.Row]:
        with self._db.connect() as connection:
            return connection.execute(
                "SELECT id, status, artifact_id, latency_ms FROM agent_runs WHERE idempotency_key=?",
                (idem_key,),
            ).fetchone()

    def _record_run(
        self,
        *,
        idem_key: str,
        agent_type: str,
        profile: ExecutionProfile,
        prompt_version: str,
        input_hash: str,
        issuer_id: Optional[int],
        case_id: Optional[str],
        deal_id: Optional[int],
        source_data_cutoff: Optional[str],
        status: str,
        error_type: Optional[str] = None,
        safe_error_message: Optional[str] = None,
    ) -> int:
        now = utc_now()
        with self._db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs (
                    case_id, issuer_id, deal_id, agent_type, provider, model,
                    thinking_enabled, reasoning_effort, prompt_version,
                    schema_version, execution_profile_version,
                    execution_profile_json, input_hash, source_data_cutoff,
                    status, started_at, error_type, safe_error_message,
                    idempotency_key, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status=excluded.status,
                    started_at=excluded.started_at,
                    completed_at=NULL,
                    error_type=excluded.error_type,
                    safe_error_message=excluded.safe_error_message
                RETURNING id
                """,
                (
                    case_id,
                    issuer_id,
                    deal_id,
                    agent_type,
                    profile.provider,
                    profile.model,
                    1 if profile.thinking_enabled else 0,
                    profile.reasoning_effort,
                    prompt_version,
                    "v1",
                    profile.profile_version,
                    json.dumps(profile.to_dict(), ensure_ascii=False),
                    input_hash,
                    source_data_cutoff,
                    status,
                    now,
                    error_type,
                    safe_error_message,
                    idem_key,
                    now,
                ),
            )
            return int(cursor.fetchone()[0])

    def _finish_run(
        self,
        run_id: int,
        *,
        status: str,
        latency_ms: int,
        retry_count: int,
        response: Optional[ProviderResponse] = None,
        artifact_id: Optional[int] = None,
        confidence: Optional[float] = None,
        error_type: Optional[str] = None,
        safe_error_message: Optional[str] = None,
    ) -> None:
        usage = response.usage if response else None
        estimated_cost = None
        if response is not None and usage is not None and self._cost_tracking:
            estimated_cost = self._estimate_cost(response.provider, response.model, usage)
        with self._db.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status=?, completed_at=?, latency_ms=?, retry_count=?,
                    input_tokens=?, cached_input_tokens=?, output_tokens=?,
                    estimated_cost_usd=?, provider_request_id=?,
                    artifact_id=?, confidence=?, error_type=?, safe_error_message=?
                WHERE id=?
                """,
                (
                    status,
                    utc_now(),
                    latency_ms,
                    retry_count,
                    usage.input_tokens if usage else None,
                    usage.cached_input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    estimated_cost,
                    response.request_id if response else None,
                    artifact_id,
                    confidence,
                    error_type,
                    safe_error_message,
                    run_id,
                ),
            )

    def _estimate_cost(self, provider: str, model: str, usage: Any) -> Optional[float]:
        key = f"{provider}/{model}"
        in_rate = self._cost_input.get(key)
        out_rate = self._cost_output.get(key)
        if in_rate is None and out_rate is None:
            return None
        billable_input = max(0, usage.input_tokens - usage.cached_input_tokens)
        return round(
            (billable_input / 1000.0) * (in_rate or 0.0)
            + (usage.output_tokens / 1000.0) * (out_rate or 0.0),
            6,
        )

    def _store_artifact(
        self,
        *,
        agent_type: str,
        artifact_kind: str,
        issuer_id: Optional[int],
        case_id: Optional[str],
        content: Any,
        confidence: Optional[float],
        evidence: Sequence[Any],
        source_data_cutoff: Optional[str],
    ) -> int:
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._db.connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM agent_artifacts
                WHERE agent_type=? AND artifact_kind=? AND content_sha256=?
                """,
                (agent_type, artifact_kind, content_hash),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            row = connection.execute(
                """
                SELECT COALESCE(MAX(version_no), 0) + 1
                FROM agent_artifacts
                WHERE agent_type=? AND artifact_kind=?
                  AND COALESCE(issuer_id, -1)=COALESCE(?, -1)
                """,
                (agent_type, artifact_kind, issuer_id),
            ).fetchone()
            version_no = int(row[0])
            cursor = connection.execute(
                """
                INSERT INTO agent_artifacts (
                    issuer_id, case_id, agent_type, artifact_kind, version_no,
                    content_json, content_sha256, confidence, evidence_json,
                    source_data_cutoff, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    issuer_id,
                    case_id,
                    agent_type,
                    artifact_kind,
                    version_no,
                    content_json,
                    content_hash,
                    confidence,
                    json.dumps(list(evidence), ensure_ascii=False),
                    source_data_cutoff,
                    now,
                ),
            )
            return int(cursor.lastrowid)


__all__ = [
    "AgentRunOutcome",
    "AgentRunner",
    "POOL_LIMITS",
    "compute_idempotency_key",
    "compute_input_hash",
    "pool_semaphore",
    "reset_pools",
]
