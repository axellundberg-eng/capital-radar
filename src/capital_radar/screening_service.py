"""Screening batch-tjänst för Capital Radar.

Hanterar skapande, uppföljning och beslut för screening-batchar.
Använder befintliga tabeller screening_batches och screening_batch_items.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database


@dataclass
class ScreeningBatchError(Exception):
    """Fel vid screening batch-operation."""

    message: str
    code: str = "screening_error"
    details: list[dict[str, Any]] | None = None

    def __str__(self) -> str:
        return self.message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_screening_batch(
    db: Database,
    *,
    mode: str,
    filters: dict[str, Any],
    note: str | None,
    created_by: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Skapa en ny screening batch.

    Om idempotency_key anges och redan finns returneras befintlig batch.
    """
    now = _utc_now()

    with db.connect() as conn:
        # Kontrollera idempotency via error-fältet (temporär lösning)
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT id, status, created_at, refresh_mfn, prepared_only
                FROM screening_batches
                WHERE error = ? AND status IN ('queued', 'running')
                ORDER BY id DESC LIMIT 1
                """,
                (f"idempotency:{idempotency_key}",),
            ).fetchone()
            if existing:
                return {
                    "id": existing["id"],
                    "status": existing["status"],
                    "mode": mode,
                    "filters": filters,
                    "note": note,
                    "created_at": existing["created_at"],
                    "idempotent_replay": True,
                }

        # Hämta flaggade bolag baserat på filter
        flagged = _find_flagged_issuers(conn, filters)

        # Skapa ny batch
        # Använd error-fältet för idempotency-nyckel om angiven
        error_value = f"idempotency:{idempotency_key}" if idempotency_key else note
        cursor = conn.execute(
            """
            INSERT INTO screening_batches (
                status, refresh_mfn, prepared_only, max_api_calls,
                total_count, created_at, updated_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "running",
                1,  # refresh_mfn
                1,  # prepared_only
                filters.get("max_api_calls", 10),
                len(flagged),
                now,
                now,
                error_value,
            ),
        )
        batch_id = cursor.lastrowid

        # Spara items för varje flaggat bolag
        for issuer_id in flagged:
            conn.execute(
                """
                INSERT INTO screening_batch_items (
                    batch_id, issuer_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (batch_id, issuer_id, "pending", now, now),
            )

        conn.commit()

        return {
            "id": batch_id,
            "status": "running",
            "mode": mode,
            "filters": filters,
            "note": note,
            "created_at": now,
            "flagged_count": len(flagged),
            "idempotent_replay": False,
        }


def get_screening_batch(db: Database, batch_id: int) -> dict[str, Any]:
    """Hämta status för en screening batch."""
    with db.connect() as conn:
        batch = conn.execute(
            """
            SELECT id, status, refresh_mfn, prepared_only, max_api_calls,
                   api_calls_used, total_count, created_at, started_at,
                   updated_at, completed_at, error
            FROM screening_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()

        if not batch:
            raise ScreeningBatchError(
                f"Batch {batch_id} hittades inte",
                code="batch_not_found",
            )

        items = conn.execute(
            """
            SELECT issuer_id, status, decision, need_score, need_timing,
                   runway_months, reason, created_at, updated_at, completed_at
            FROM screening_batch_items
            WHERE batch_id = ?
            ORDER BY issuer_id
            """,
            (batch_id,),
        ).fetchall()

        return {
            "id": batch["id"],
            "status": batch["status"],
            "mode": "incremental" if batch["prepared_only"] else "full",
            "filters": {
                "refresh_mfn": bool(batch["refresh_mfn"]),
                "max_api_calls": batch["max_api_calls"],
            },
            "note": batch["error"],
            "created_at": batch["created_at"],
            "started_at": batch["started_at"],
            "completed_at": batch["completed_at"],
            "api_calls_used": batch["api_calls_used"],
            "total_count": batch["total_count"],
            "flagged_issuers": [dict(i) for i in items],
        }


def update_screening_decision(
    db: Database,
    batch_id: int,
    issuer_id: int,
    payload: dict[str, Any],
    *,
    changed_by: str,
    session_id: str | None,
) -> dict[str, Any]:
    """Uppdatera beslut för ett bolag i en screening batch.

    Senaste beslutet vinner (overwrite).
    """
    now = _utc_now()

    # Validera obligatoriska fält
    decision = payload.get("capital_need_decision")
    if decision not in {"yes", "no", "pending"}:
        raise ScreeningBatchError(
            "capital_need_decision måste vara yes, no eller pending",
            code="validation",
            details=[{"field": "capital_need_decision", "error": "invalid_value"}],
        )

    with db.connect() as conn:
        # Kontrollera att batchen finns
        batch = conn.execute(
            "SELECT id, status FROM screening_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise ScreeningBatchError(
                f"Batch {batch_id} hittades inte",
                code="batch_not_found",
            )

        # Kontrollera att bolaget finns i batchen
        existing = conn.execute(
            """
            SELECT id FROM screening_batch_items
            WHERE batch_id = ? AND issuer_id = ?
            """,
            (batch_id, issuer_id),
        ).fetchone()

        if not existing:
            raise ScreeningBatchError(
                f"Bolag {issuer_id} finns inte i batch {batch_id}",
                code="issuer_not_in_batch",
            )

        # Uppdatera beslutet (senaste vinner)
        conn.execute(
            """
            UPDATE screening_batch_items
            SET decision = ?,
                need_score = ?,
                need_timing = ?,
                reason = ?,
                updated_at = ?
            WHERE batch_id = ? AND issuer_id = ?
            """,
            (
                decision,
                payload.get("need_score"),
                payload.get("need_timing"),
                payload.get("note"),
                now,
                batch_id,
                issuer_id,
            ),
        )
        conn.commit()

        # Hämta uppdaterat beslut
        updated = conn.execute(
            """
            SELECT issuer_id, status, decision, need_score, need_timing,
                   runway_months, reason, updated_at
            FROM screening_batch_items
            WHERE batch_id = ? AND issuer_id = ?
            """,
            (batch_id, issuer_id),
        ).fetchone()

        return {
            "batch_id": batch_id,
            "issuer_id": issuer_id,
            "decision": dict(updated),
            "updated_at": now,
        }


def _find_flagged_issuers(
    conn: sqlite3.Connection,
    filters: dict[str, Any],
) -> list[int]:
    """Hitta bolag som matchar screening-filtret."""
    conditions = []
    params: list[Any] = []

    if "runway_max" in filters:
        conditions.append("r.adjusted_runway_months <= ?")
        params.append(float(filters["runway_max"]))

    if "fit_min" in filters:
        conditions.append("w.bergs_fit_score >= ?")
        params.append(int(filters["fit_min"]))

    if not conditions:
        # Standard: alla bolag med runway-data
        conditions.append("r.adjusted_runway_months IS NOT NULL")

    query = f"""
        SELECT DISTINCT i.id
        FROM issuers i
        JOIN runway_assessments r ON r.issuer_id = i.id
        LEFT JOIN issuer_watchlist w ON w.issuer_id = i.id
        WHERE {' AND '.join(conditions)}
        ORDER BY i.id
        LIMIT 500
    """

    rows = conn.execute(query, params).fetchall()
    return [row[0] for row in rows]
