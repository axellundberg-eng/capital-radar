from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from ..analysis_store import AnalysisStore
from ..config import Settings
from ..documents import (
    DocumentError,
    SafeDocumentFetcher,
    extract_document,
)
from ..document_quality import evaluate_document_quality
from ..execution import (
    STATUS_COMPLETED,
    WORK_SOURCE_FETCH,
    WorkItem,
    WorkPool,
    WorkResult,
)
from ..normalization import stable_hash

LOGGER = logging.getLogger("bergs.ai")


def _is_supported_document_target(url: str, mime_type: str | None) -> bool:
    mime = str(mime_type or "").casefold()
    path = urlsplit(url).path.casefold()
    if mime.startswith("image/") or mime in {
        "application/zip",
        "application/x-zip-compressed",
    }:
        return False
    return not path.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip")
    )


def _process_candidate(
    store: AnalysisStore,
    settings: Settings,
    fetcher: SafeDocumentFetcher,
    candidate: Any,
    *,
    max_targets_per_document: int,
) -> dict[str, Any]:
    """Hämta + extrahera + persistera EN kandidat (blockerande I/O + CPU).

    Detta är den feature-specifika handlern för source-fetch-poolen. Den är
    synkron och körs via ``asyncio.to_thread`` så att event-loopen aldrig
    blockeras av HTTP-nedladdning eller PDF-parsning. Failure isolation
    sköts av WorkPool — ett trasigt dokument stoppar aldrig batchen.
    """
    document_result: dict[str, Any] = {
        "document_id": candidate.document_id,
        "issuer": candidate.issuer_name,
        "title": candidate.title,
        "attachments": [],
    }
    last_error = "Dokumentet saknade användbar URL"
    supported_targets = []
    for target in candidate.urls:
        if _is_supported_document_target(target.url, target.mime_type):
            supported_targets.append(target)
            continue
        if target.content_status != "unsupported":
            store.save_content(
                candidate=candidate,
                attachment_id=target.attachment_id,
                source_url=target.url,
                final_url=None,
                local_path=None,
                mime_type=target.mime_type,
                sha256=None,
                byte_size=None,
                extraction_method=None,
                extracted_text=None,
                text_sha256=None,
                page_count=None,
                status="unsupported",
                error="Bilagan är inte ett PDF- eller HTML-underlag",
            )
        document_result["attachments"].append(
            {
                "source_url": target.url,
                "status": "unsupported",
            }
        )
    fetch_targets = supported_targets[: max(1, max_targets_per_document)]
    deferred_targets = supported_targets[len(fetch_targets) :]
    for target in deferred_targets:
        document_result["attachments"].append(
            {
                "source_url": target.url,
                "status": "deferred",
            }
        )
    for target in fetch_targets:
        if target.content_status in {"extracted", "needs_ocr", "too_short"}:
            document_result["attachments"].append(
                {
                    "source_url": target.url,
                    "status": "already_prepared",
                }
            )
            continue
        try:
            downloaded = fetcher.get(target.url)
            extracted = extract_document(
                downloaded.body,
                downloaded.mime_type or target.mime_type,
                downloaded.final_url,
            )
            quality = evaluate_document_quality(
                extracted.text,
                profile_category=candidate.profile_category,
                page_count=extracted.page_count,
            )
            # Spara ALDRIG PDF-filer på disk — vi extraherar text direkt i minnet
            # och sparar bara den strukturerade analysdatan. Detta förhindrar att
            # Railway-volymen fylls av PDF:er.
            digest = stable_hash(downloaded.body)
            store.save_content(
                candidate=candidate,
                attachment_id=target.attachment_id,
                source_url=target.url,
                final_url=downloaded.final_url,
                local_path=None,
                mime_type=downloaded.mime_type,
                sha256=digest,
                byte_size=len(downloaded.body),
                extraction_method=extracted.method,
                extracted_text=extracted.text,
                text_sha256=stable_hash(extracted.text),
                page_count=extracted.page_count,
                status=extracted.status,
                quality_score=quality.score,
                quality_status=quality.status,
                quality_reason=quality.reason,
            )
            document_result["attachments"].append(
                {
                    "status": extracted.status,
                    "pages": extracted.page_count,
                    "characters": len(extracted.text),
                    "source_url": target.url,
                    "quality_status": quality.status,
                    "quality_score": quality.score,
                    "quality_reason": quality.reason,
                }
            )
            LOGGER.info(
                "doc_fetch_ok document=%s url=%s status=%s pages=%s chars=%s",
                candidate.document_id,
                target.url,
                extracted.status,
                extracted.page_count,
                len(extracted.text),
            )
        except DocumentError as exc:
            last_error = str(exc)
            LOGGER.warning(
                "doc_fetch_failed document=%s url=%s error=%s",
                candidate.document_id,
                target.url,
                last_error,
            )
            store.save_content(
                candidate=candidate,
                attachment_id=target.attachment_id,
                source_url=target.url,
                final_url=None,
                local_path=None,
                mime_type=target.mime_type,
                sha256=None,
                byte_size=None,
                extraction_method=None,
                extracted_text=None,
                text_sha256=None,
                page_count=None,
                status="failed",
                error=last_error,
            )
            document_result["attachments"].append(
                {
                    "source_url": target.url,
                    "status": "failed",
                    "error": last_error,
                }
            )
    store.regrade_document_quality(candidate.document_id)
    readiness = store.document_readiness(candidate.document_id)
    document_result.update(
        {
            "status": readiness["readiness"],
            "reason": readiness["reason"],
            "selected_source_url": readiness.get("source_url"),
            "pages": readiness.get("page_count"),
            "characters": readiness.get("char_count"),
        }
    )
    return document_result


def prepare_documents(
    store: AnalysisStore,
    settings: Settings,
    *,
    limit: int,
    issuer_id: int | None = None,
    document_id: int | None = None,
    prefer_reports: bool = False,
    request_timeout: float | None = None,
    max_targets_per_document: int = 3,
) -> dict[str, Any]:
    """Synkron kant: kör den async parallella dokument-preparen.

    Hot path är ``prepare_documents_async`` — denna wrapper kör den på en
    egen event loop för sync callers (CLI, manual actions, report refresh).
    """
    return asyncio.run(
        prepare_documents_async(
            store,
            settings,
            limit=limit,
            issuer_id=issuer_id,
            document_id=document_id,
            prefer_reports=prefer_reports,
            request_timeout=request_timeout,
            max_targets_per_document=max_targets_per_document,
        )
    )


async def prepare_documents_async(
    store: AnalysisStore,
    settings: Settings,
    *,
    limit: int,
    issuer_id: int | None = None,
    document_id: int | None = None,
    prefer_reports: bool = False,
    request_timeout: float | None = None,
    max_targets_per_document: int = 3,
) -> dict[str, Any]:
    """Bounded parallell dokument-prepare via en source-fetch WorkPool.

    Varje kandidat blir ett WorkItem; blockerande HTTP-nedladdning och
    PDF-parsning körs via ``asyncio.to_thread`` så att upp till
    ``source_fetch_concurrency`` dokument hämtas samtidigt. DB-writes
    (``save_content``) är korta och sker per dokument, utanför nätverks-
    väntan. Ett trasigt dokument stoppar aldrig batchen.
    """
    fetcher = SafeDocumentFetcher(
        allowed_hosts=settings.document_allowed_hosts,
        user_agent=settings.user_agent,
        timeout=(
            settings.http_timeout
            if request_timeout is None
            else max(3.0, min(request_timeout, settings.http_timeout))
        ),
        max_bytes=settings.document_max_bytes,
        retries=settings.document_fetch_retries,
        retry_backoff=settings.document_fetch_retry_backoff,
    )
    candidates = store.fetch_candidates(
        limit,
        issuer_id=issuer_id,
        document_id=document_id,
        prefer_reports=prefer_reports,
    )
    output: dict[str, Any] = {
        "seen": len(candidates),
        "prepared": 0,
        "needs_ocr": 0,
        "failed": 0,
        "documents": [],
    }
    if not candidates:
        return output

    # Ett WorkItem per kandidat. Payloaden refererar document_id (persisted
    # state) — vi duplicerar inte dokumentdata genom kön.
    work_items = [
        WorkItem(
            kind=WORK_SOURCE_FETCH,
            handler="prepare_document",
            work_id=f"prepare-doc-{candidate.document_id}",
            issuer_id=candidate.issuer_id,
            document_id=candidate.document_id,
            max_provider_calls=0,  # ingen AI/DeepSeek — bara källhämtning
        )
        for candidate in candidates
    ]
    candidates_by_work_id = {
        item.work_id: candidate for item, candidate in zip(work_items, candidates)
    }

    async def _handle(work_item: WorkItem) -> WorkResult:
        candidate = candidates_by_work_id[work_item.work_id]
        document_result = await asyncio.to_thread(
            _process_candidate,
            store,
            settings,
            fetcher,
            candidate,
            max_targets_per_document=max_targets_per_document,
        )
        return WorkResult(
            work_id=work_item.work_id,
            status=STATUS_COMPLETED,
            output=document_result,
        )

    concurrency = max(1, settings.source_fetch_concurrency)
    pool = WorkPool(
        name=WORK_SOURCE_FETCH,
        handler=_handle,
        concurrency=concurrency,
        max_queue=max(concurrency, settings.source_fetch_max_queue),
        timeout=float(settings.source_fetch_timeout),
    )
    async with pool:
        work_results = await pool.run_batch(work_items)
    output["executor_metrics"] = pool.metrics.snapshot()

    for work_result in work_results:
        if work_result.status == STATUS_COMPLETED:
            document_result = work_result.output
        else:
            # Failure isolation: mappa pool-fel till ett failed-dokument så
            # att output-kontraktet alltid har samma form som före.
            candidate = candidates_by_work_id[work_result.work_id]
            document_result = {
                "document_id": candidate.document_id,
                "issuer": candidate.issuer_name,
                "title": candidate.title,
                "attachments": [],
                "status": "failed",
                "reason": work_result.error or "Dokument-prepare misslyckades",
            }
        readiness = document_result.get("status")
        if readiness == "complete":
            output["prepared"] += 1
        elif readiness == "needs_ocr":
            output["needs_ocr"] += 1
        else:
            output["failed"] += 1
        output["documents"].append(document_result)
    return output
