from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .ai.deepseek_v2 import (
    PROFILE_FLASH_EXTRACTION,
    build_async_client,
    resolve_profile,
)
from .analysis_store import AnalysisStore
from .config import Settings
from .execution import (
    STATUS_COMPLETED,
    WORK_FLASH_EXTRACTION,
    WorkItem,
    WorkPool,
    WorkResult,
)
from .extraction_v2 import (
    PROMPT_VERSION_V2,
    SYSTEM_PROMPT_V2,
    run_extraction_v2_async,
)
from .normalization import stable_hash
from .text_selection import select_relevant_text


def analyze_documents(
    store: AnalysisStore,
    settings: Settings,
    *,
    limit: int,
    dry_run: bool,
    document_id: int | None = None,
    issuer_id: int | None = None,
    prefer_reports: bool = False,
    deadline_at: str | None = None,
    candidates: list[Any] | None = None,
    before_provider_call: Callable[[], bool] | None = None,
    progress_callback: (
        Callable[[str, dict[str, Any]], bool | None] | None
    ) = None,
    force_reinterpret: bool = False,
    force_key: str | None = None,
) -> dict[str, Any]:
    """Synkron kant: kör den async canonical extraction-pipelinen.

    Hot path är ``analyze_documents_async`` — denna wrapper kör den på en
    egen event loop för sync callers (CLI, manual actions, tester).
    """
    return asyncio.run(
        analyze_documents_async(
            store,
            settings,
            limit=limit,
            dry_run=dry_run,
            document_id=document_id,
            issuer_id=issuer_id,
            prefer_reports=prefer_reports,
            deadline_at=deadline_at,
            candidates=candidates,
            before_provider_call=before_provider_call,
            progress_callback=progress_callback,
            force_reinterpret=force_reinterpret,
            force_key=force_key,
        )
    )


async def analyze_documents_async(
    store: AnalysisStore,
    settings: Settings,
    *,
    limit: int,
    dry_run: bool,
    document_id: int | None = None,
    issuer_id: int | None = None,
    prefer_reports: bool = False,
    deadline_at: str | None = None,
    candidates: list[Any] | None = None,
    before_provider_call: Callable[[], bool] | None = None,
    progress_callback: (
        Callable[[str, dict[str, Any]], bool | None] | None
    ) = None,
    force_reinterpret: bool = False,
    force_key: str | None = None,
) -> dict[str, Any]:
    """Canonical bounded-parallell extraction.

    Flöde per dokument: texturval + input_hash → ``create_job`` (durable
    identitet + idempotency FÖRE nätverk) → admission/cost check → WorkItem
    → bounded WorkPool (queue + workers + hard timeout) → DeepSeek Flash →
    validera → persist per dokument. Ett trasigt dokument stoppar aldrig
    batchen, och en långsam request blockerar aldrig övriga workers.

    Om ``candidates`` ges används den listan direkt (t.ex. post-rapport-PM)
    istället för att välja via ``analysis_candidates``.
    """
    recovered = store.recover_stale_jobs()
    if candidates is None:
        candidates = store.analysis_candidates(
            100 if document_id else limit,
            issuer_id=issuer_id,
            prefer_reports=prefer_reports,
        )
    if document_id is not None:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.document_id == document_id
        ][:limit]
    result: dict[str, Any] = {
        "seen": len(candidates),
        "completed": 0,
        "skipped_idempotent": 0,
        "already_running": 0,
        "failed": 0,
        "dry_run": dry_run,
        "estimated_input_tokens": 0,
        "recovered_stale_jobs": recovered,
        "provider_error": None,
        "api_limit_reached": False,
        "documents": [],
    }
    if not dry_run and not settings.deepseek_api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY saknas. Lägg nyckeln i miljön innan extraktion."
        )
    absolute_deadline = (
        datetime.fromisoformat(deadline_at)
        if deadline_at
        else datetime.now(timezone.utc)
        + timedelta(seconds=settings.extraction_hard_timeout + 10)
    )
    deadline_text = absolute_deadline.replace(microsecond=0).isoformat()
    active_model = resolve_profile(settings, PROFILE_FLASH_EXTRACTION).model
    active_provider = "deepseek"

    # Fas 1: förbered, skapa durable jobb och gör idempotency/admission
    # FÖRE något nätverksanrop. Endast dokument med aktiv lease blir WorkItems.
    prepared: list[tuple[Any, Any, int, str, dict[str, Any]]] = []
    for candidate in candidates:
        selection = select_relevant_text(
            candidate.extracted_text,
            max_chars=settings.extraction_max_input_chars,
            document_title=candidate.title,
            max_report_pages=settings.extraction_max_report_pages,
        )
        # Speglar classification-hinten i _user_prompt_v2: när dokumentet är
        # klassificerat som en finansiell rapport (profile_category='report')
        # får prompten en extra rad. Då måste input_hash skilja sig från en
        # extraction som kördes utan hinten — annars skulle en rapport som
        # tidigare missklassificerats (is_financial_report=0) hoppas över som
        # idempotent och aldrig köras om med den förbättrade prompten.
        # Hinten läggs sist så att icke-rapporters hash är oförändrad och de
        # inte tvingas köras om.
        report_hint = (
            "report"
            if getattr(candidate, "profile_category", None) == "report"
            else None
        )
        if report_hint or force_reinterpret:
            input_hash = stable_hash(
                PROMPT_VERSION_V2,
                active_model,
                SYSTEM_PROMPT_V2,
                candidate.title,
                candidate.published_at,
                selection.text,
                report_hint,
                force_key if force_reinterpret else None,
            )
        else:
            input_hash = stable_hash(
                PROMPT_VERSION_V2,
                active_model,
                SYSTEM_PROMPT_V2,
                candidate.title,
                candidate.published_at,
                selection.text,
            )
        result["estimated_input_tokens"] += selection.estimated_tokens
        item: dict[str, Any] = {
            "document_id": candidate.document_id,
            "issuer": candidate.issuer_name,
            "title": candidate.title,
            "selected_pages": selection.selected_pages,
            "selected_characters": selection.selected_char_count,
            "estimated_input_tokens": selection.estimated_tokens,
        }
        if progress_callback is not None:
            progress_callback(
                "preparing_text",
                {
                    "document_id": candidate.document_id,
                    "document_title": candidate.title,
                    "source_url": candidate.source_url,
                    "estimated_input_tokens": selection.estimated_tokens,
                    "selected_pages": selection.selected_pages,
                    "selected_characters": selection.selected_char_count,
                },
            )
        if dry_run:
            item["status"] = "ready"
            result["documents"].append(item)
            continue

        job_id, job_status, lease_token = store.create_job(
            candidate=candidate,
            model=active_model,
            prompt_version=PROMPT_VERSION_V2,
            input_sha256=input_hash,
            selected_char_count=selection.selected_char_count,
            estimated_input_tokens=selection.estimated_tokens,
            deadline_at=deadline_text,
            provider=active_provider,
        )
        item["job_id"] = job_id
        if job_status == "completed":
            item["status"] = "skipped_idempotent"
            result["skipped_idempotent"] += 1
            result["documents"].append(item)
            continue
        if job_status == "running":
            item["status"] = "already_running"
            result["already_running"] += 1
            result["documents"].append(item)
            continue
        if lease_token is None:
            item.update(
                {
                    "status": "failed",
                    "error": "AI-jobbet saknade en giltig lease",
                }
            )
            result["failed"] += 1
            result["documents"].append(item)
            continue
        if before_provider_call is not None and not before_provider_call():
            reason = (
                "Batchens hårda tak för nya AI-anrop är nått. "
                "Inget API-anrop gjordes."
            )
            store.fail_job(job_id, lease_token, reason)
            item.update({"status": "deferred", "error": reason})
            result["api_limit_reached"] = True
            result["documents"].append(item)
            # Admission stoppad: lägg inte till fler nya jobb i kön.
            break
        prepared.append((candidate, selection, job_id, lease_token, item))

    if dry_run or not prepared:
        return result

    # Fas 2: EN delad långlivad AsyncOpenAI-klient för hela batchen
    # (application lifecycle — aldrig client-per-document).
    client = build_async_client(settings)
    items_by_work_id: dict[str, dict[str, Any]] = {}
    try:
        work_items: list[WorkItem] = []
        handlers: dict[str, tuple[Any, Any, int, str]] = {}
        for candidate, selection, job_id, lease_token, item in prepared:
            work_id = f"extraction-job-{job_id}"
            items_by_work_id[work_id] = item
            handlers[work_id] = (candidate, selection, job_id, lease_token)
            work_items.append(
                WorkItem(
                    kind=WORK_FLASH_EXTRACTION,
                    handler="extraction_v2",
                    work_id=work_id,
                    issuer_id=candidate.issuer_id,
                    document_id=candidate.document_id,
                    profile=PROFILE_FLASH_EXTRACTION,
                    max_provider_calls=1,
                )
            )

        async def _handle(work_item: WorkItem) -> WorkResult:
            candidate, selection, job_id, lease_token = handlers[
                work_item.work_id
            ]
            outcome = await run_extraction_v2_async(
                store,
                settings,
                candidate=candidate,
                selection=selection,
                job_id=job_id,
                lease_token=lease_token,
                progress_callback=progress_callback,
                client=client,
            )
            usage = outcome.get("usage") or {}
            status = outcome.get("status", "failed")
            return WorkResult(
                work_id=work_item.work_id,
                status=(
                    STATUS_COMPLETED if status == "completed" else status
                ),
                output=outcome,
                provider="deepseek",
                model=outcome.get("model"),
                input_tokens=usage.get("prompt_tokens"),
                cached_tokens=usage.get("cached_tokens"),
                output_tokens=usage.get("completion_tokens"),
                provider_calls=int(outcome.get("provider_calls", 0)),
                error=outcome.get("error"),
                error_type=(
                    None
                    if status == "completed"
                    else outcome.get("error_type", "extraction_failed")
                ),
            )

        pool = WorkPool(
            name=WORK_FLASH_EXTRACTION,
            handler=_handle,
            concurrency=max(1, settings.deepseek_flash_max_concurrency),
            max_queue=max(
                settings.deepseek_flash_max_concurrency,
                settings.extraction_max_queue,
            ),
            timeout=float(settings.deepseek_flash_hard_timeout_seconds),
        )
        async with pool:
            work_results = await pool.run_batch(work_items)
        result["executor_metrics"] = pool.metrics.snapshot()
    finally:
        await client.close()

    # Fas 3: mappa canonical WorkResult tillbaka till per-dokument-items.
    for work_result in work_results:
        item = items_by_work_id[work_result.work_id]
        outcome = work_result.output or {}
        if work_result.status == STATUS_COMPLETED:
            item.update(outcome)
            item["status"] = "completed"
            result["completed"] += 1
        else:
            item.update(outcome)
            item["status"] = work_result.status
            item["error"] = work_result.error or outcome.get("error")
            result["failed"] += 1
            if item["error"]:
                result["provider_error"] = item["error"]
        result["documents"].append(item)
    return result
