"""Granskarens HTTP-API i Capital Radar-appen (``/review``).

Granskaren pratar bara med databasen härigenom. Endpoints läser DeepSeeks
data utan att ändra den och skriver bara review_runs/review_verdicts.
Skyddas med bearer-token (SCREENER_REVIEW_API_TOKEN); utan token är API:t
avstängt (503) och resten av appen påverkas inte.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..db import Database
from . import prompts
from .store import (
    RESULT_STATUSES,
    ReviewStoreError,
    SqliteReviewStore,
    extraction_input_to_dict,
)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Literal["review_extraction", "review_headlines"]
    provider: str
    model: str
    prompt_version: str
    input_sha256: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=64, max_length=64)
    issuer_id: Optional[int] = None
    document_id: Optional[int] = None
    extraction_job_id: Optional[int] = None
    estimated_input_tokens: int = 0
    hard_timeout_seconds: float = Field(gt=0, le=180)
    retry_failed: bool = False


class VerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["financial_fact", "financing_event", "document"]
    target_id: int
    verdict: Literal["confirmed", "disputed", "uncertain", "flag", "no_flag"]
    issues: list[str] = Field(default_factory=list)
    reasoning: str
    source_quote: str = ""
    quote_found: Optional[int] = None
    expected_value: Optional[str] = None


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    error_type: Optional[str] = None
    error: Optional[str] = None
    response_id: Optional[str] = None
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    raw_output: Optional[str] = None
    verdicts: list[VerdictIn] = Field(default_factory=list)


def build_review_router(settings: Any, *, store: SqliteReviewStore | None = None) -> APIRouter:
    router = APIRouter(prefix="/review", tags=["granskning"])
    review_store = store or SqliteReviewStore(Database(settings.db_path))

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = getattr(settings, "review_api_token", None)
        if not expected:
            raise HTTPException(503, "Granskar-API:t är avstängt (SCREENER_REVIEW_API_TOKEN saknas)")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "Ogiltig token")

    @router.get("/extraction-candidates", dependencies=[Depends(require_token)])
    def extraction_candidates(
        model: str,
        prompt_version: str,
        limit: int = Query(10, ge=1, le=200),
        retry_failed: bool = False,
        max_source_chars: int = Query(24_000, ge=2_000, le=200_000),
    ) -> dict[str, Any]:
        items = review_store.list_extraction_inputs(
            task=prompts.EXTRACTION_TASK, model=model, prompt_version=prompt_version,
            limit=limit, retry_failed=retry_failed, max_source_chars=max_source_chars,
        )
        return {"items": [extraction_input_to_dict(item) for item in items]}

    @router.get("/headline-candidates", dependencies=[Depends(require_token)])
    def headline_candidates(
        model: str,
        prompt_version: str,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        items = review_store.list_headline_candidates(
            task=prompts.HEADLINE_TASK, model=model,
            prompt_version=prompt_version, limit=limit,
        )
        return {"items": [asdict(item) for item in items]}

    @router.post("/runs", dependencies=[Depends(require_token)])
    def claim_run(request: ClaimRequest) -> dict[str, Any]:
        run_id = review_store.claim(request.model_dump())
        if run_id is None:
            raise HTTPException(409, "Samma indata är redan granskad eller pågår")
        return {"run_id": run_id}

    @router.post("/runs/{run_id}/result", dependencies=[Depends(require_token)])
    def finish_run(run_id: int, request: ResultRequest) -> dict[str, Any]:
        if request.status not in RESULT_STATUSES:
            raise HTTPException(422, f"Ogiltig status: {request.status}")
        payload = request.model_dump()
        try:
            review_store.finish(run_id, payload)
        except ReviewStoreError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "status": request.status}

    return router
