"""Lagring för granskaren, bakom ett gemensamt gränssnitt.

``SqliteReviewStore`` körs i Capital Radar-appen (bakom API:t) och är det
enda som rör databasen. ``HttpReviewStore`` är granskarens klient: den
pratar bara med appens API under ``/review`` och öppnar aldrig SQLite.

Båda läser DeepSeeks tabeller utan att skriva i dem; skrivningar går bara
till review_runs och review_verdicts.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..db import Database
from ..normalization import utc_now
from .inputs import (
    ExtractionInput,
    HeadlineCandidate,
    list_filtered_headlines,
    list_reviewable_jobs,
    load_extraction_input,
)

RETRYABLE_STATUSES = frozenset({"timeout", "rate_limited", "failed"})
RESULT_STATUSES = frozenset({
    "completed", "incomplete", "validation_failed", "refused",
    "timeout", "rate_limited", "failed",
})


class ReviewStoreError(Exception):
    """Lagringen (API eller databas) gick inte att nå eller svarade fel."""


class ReviewStore(Protocol):
    def list_extraction_inputs(
        self, *, task: str, model: str, prompt_version: str, limit: int,
        retry_failed: bool, max_source_chars: int,
    ) -> list[ExtractionInput]: ...

    def list_headline_candidates(
        self, *, task: str, model: str, prompt_version: str, limit: int,
    ) -> list[HeadlineCandidate]: ...

    def claim(self, claim: dict[str, Any]) -> Optional[int]: ...

    def finish(self, run_id: int, result: dict[str, Any]) -> None: ...


class SqliteReviewStore:
    def __init__(self, db: Database, *, clock: Callable[[], str] = utc_now) -> None:
        self.db = db
        self._clock = clock

    def list_extraction_inputs(
        self, *, task: str, model: str, prompt_version: str, limit: int,
        retry_failed: bool, max_source_chars: int,
    ) -> list[ExtractionInput]:
        with self.db.connect() as connection:
            job_ids = list_reviewable_jobs(
                connection, task=task, model=model, prompt_version=prompt_version,
                limit=limit, retry_failed=retry_failed, now=self._clock(),
            )
            items = [
                load_extraction_input(connection, job_id, max_source_chars=max_source_chars)
                for job_id in job_ids
            ]
        return [item for item in items if item is not None]

    def list_headline_candidates(
        self, *, task: str, model: str, prompt_version: str, limit: int,
    ) -> list[HeadlineCandidate]:
        with self.db.connect() as connection:
            return list_filtered_headlines(
                connection, task=task, model=model,
                prompt_version=prompt_version, limit=limit,
            )

    def claim(self, claim: dict[str, Any]) -> Optional[int]:
        """Lägg en running-rad. None om samma indata redan granskats/körs."""
        now = self._clock()
        deadline = (
            datetime.fromisoformat(now)
            + timedelta(seconds=float(claim["hard_timeout_seconds"]))
        ).isoformat()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO review_runs (
                    task, provider, model, prompt_version, input_sha256,
                    idempotency_key, issuer_id, document_id, extraction_job_id,
                    status, estimated_input_tokens, deadline_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    claim["task"], claim["provider"], claim["model"],
                    claim["prompt_version"], claim["input_sha256"],
                    claim["idempotency_key"], claim.get("issuer_id"),
                    claim.get("document_id"), claim.get("extraction_job_id"),
                    int(claim.get("estimated_input_tokens") or 0), deadline, now,
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid)
            if not claim.get("retry_failed"):
                return None
            existing = connection.execute(
                "SELECT id, status, deadline_at FROM review_runs WHERE idempotency_key=?",
                (claim["idempotency_key"],),
            ).fetchone()
            stale = existing["status"] == "running" and (
                existing["deadline_at"] is not None and existing["deadline_at"] <= now
            )
            if existing["status"] not in RETRYABLE_STATUSES and not stale:
                return None
            cursor = connection.execute(
                """
                UPDATE review_runs
                SET status='running', attempt_count=attempt_count+1,
                    error=NULL, error_type=NULL, deadline_at=?, started_at=?
                WHERE id=? AND status=?
                """,
                (deadline, now, int(existing["id"]), existing["status"]),
            )
            return int(existing["id"]) if cursor.rowcount else None

    def finish(self, run_id: int, result: dict[str, Any]) -> None:
        status = result["status"]
        if status not in RESULT_STATUSES:
            raise ReviewStoreError(f"ogiltig status: {status}")
        now = self._clock()
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT * FROM review_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ReviewStoreError(f"okänt run: {run_id}")
            if run["status"] != "running":
                raise ReviewStoreError(f"run {run_id} är inte running ({run['status']})")
            for verdict in result.get("verdicts") or []:
                connection.execute(
                    """
                    INSERT INTO review_verdicts (
                        run_id, task, provider, model, prompt_version,
                        input_sha256, target_kind, target_id, document_id,
                        extraction_job_id, verdict, issues_json, expected_value,
                        reasoning, source_quote, quote_found_in_source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, run["task"], run["provider"], run["model"],
                        run["prompt_version"], run["input_sha256"],
                        verdict["target_kind"], int(verdict["target_id"]),
                        int(verdict["target_id"]) if verdict["target_kind"] == "document"
                        else run["document_id"],
                        run["extraction_job_id"], verdict["verdict"],
                        json.dumps(verdict.get("issues") or []),
                        verdict.get("expected_value"), verdict["reasoning"],
                        verdict.get("source_quote") or "",
                        verdict.get("quote_found"), now,
                    ),
                )
            connection.execute(
                """
                UPDATE review_runs SET
                    status=?, error_type=?, error=?, response_id=?, stop_reason=?,
                    input_tokens=?, output_tokens=?, cache_read_tokens=?,
                    latency_ms=?, raw_output=?, completed_at=?
                WHERE id=?
                """,
                (
                    status, result.get("error_type"),
                    (result.get("error") or "")[:500] or None,
                    result.get("response_id"), result.get("stop_reason"),
                    result.get("input_tokens"), result.get("output_tokens"),
                    result.get("cache_read_tokens"), result.get("latency_ms"),
                    result.get("raw_output"), now, run_id,
                ),
            )


# --------------------------------------------------------------- HTTP-klient

Transport = Callable[[str, str, Optional[dict[str, Any]]], tuple[int, Any]]


def urllib_transport(base_url: str, token: str, timeout: float) -> Transport:
    """Standardtransport: urllib med uttrycklig timeout per anrop."""
    base = base_url.rstrip("/")

    def send(method: str, path: str, body: Optional[dict[str, Any]]) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"null")
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"null")
            except ValueError:
                payload = None
            return exc.code, payload
        except (URLError, TimeoutError, OSError) as exc:
            raise ReviewStoreError(f"Capital Radar-API:t svarar inte: {exc}") from exc

    return send


class HttpReviewStore:
    """Granskarens enda väg till databasen: Capital Radar-API:t."""

    def __init__(self, transport: Transport) -> None:
        self._send = transport

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
              *, allow: tuple[int, ...] = ()) -> tuple[int, Any]:
        status, payload = self._send(method, path, body)
        if status >= 400 and status not in allow:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise ReviewStoreError(f"{method} {path} gav HTTP {status}: {detail}")
        return status, payload

    def list_extraction_inputs(
        self, *, task: str, model: str, prompt_version: str, limit: int,
        retry_failed: bool, max_source_chars: int,
    ) -> list[ExtractionInput]:
        query = urlencode({
            "model": model, "prompt_version": prompt_version, "limit": limit,
            "retry_failed": str(retry_failed).lower(), "max_source_chars": max_source_chars,
        })
        _, payload = self._call("GET", f"/review/extraction-candidates?{query}")
        return [extraction_input_from_dict(item) for item in payload["items"]]

    def list_headline_candidates(
        self, *, task: str, model: str, prompt_version: str, limit: int,
    ) -> list[HeadlineCandidate]:
        query = urlencode({"model": model, "prompt_version": prompt_version, "limit": limit})
        _, payload = self._call("GET", f"/review/headline-candidates?{query}")
        return [HeadlineCandidate(**item) for item in payload["items"]]

    def claim(self, claim: dict[str, Any]) -> Optional[int]:
        status, payload = self._call("POST", "/review/runs", claim, allow=(409,))
        return None if status == 409 else int(payload["run_id"])

    def finish(self, run_id: int, result: dict[str, Any]) -> None:
        self._call("POST", f"/review/runs/{run_id}/result", result)


def extraction_input_to_dict(item: ExtractionInput) -> dict[str, Any]:
    data = asdict(item)
    data["pages"] = [[number, text] for number, text in item.pages]
    return data


def extraction_input_from_dict(data: dict[str, Any]) -> ExtractionInput:
    return ExtractionInput(**{
        **data, "pages": [(int(number), str(text)) for number, text in data.get("pages") or []],
    })
