"""Source-bound proposals for correcting financing-event facts.

The chat/controller may create proposals, but only a reviewed proposal can
populate an empty financing field.  This keeps evidence and reversibility next
to the financial data while leaving screening calculations untouched.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping

from .normalization import utc_now


PROPOSAL_STATUSES = ("proposed", "applied", "rejected", "superseded")
TEXT_FIELDS = {
    "canonical_round_key", "lifecycle_stage", "financing_type",
    "guarantee_fee_details", "guarantee_fee_basis", "adviser", "issuer_agent", "outcome",
}
NUMBER_FIELDS = {
    "gross_amount", "net_amount", "issue_price", "subscription_commitment_amount",
    "subscription_commitment_pct", "guarantee_commitment_amount",
    "guarantee_commitment_pct", "guarantee_utilized_amount",
    "guarantee_utilized_pct", "guarantee_fee_amount", "guarantee_fee_pct",
    "guarantee_fee_cash_pct", "guarantee_fee_share_pct", "credit_limit",
    "drawn_amount", "undrawn_commitment",
    "transaction_cost",
}
ALLOWED_FIELDS = TEXT_FIELDS | NUMBER_FIELDS

FINANCING_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_field_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key TEXT NOT NULL UNIQUE,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    field_name TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    previous_value_json TEXT,
    source_document_id INTEGER NOT NULL REFERENCES documents(id),
    source_excerpt TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed', 'applied', 'rejected', 'superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    resolution_note TEXT,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_financing_field_proposals_queue
    ON financing_field_proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_financing_field_proposals_event
    ON financing_field_proposals(financing_event_id, field_name, status);
"""


class FinancingProposalError(ValueError):
    """Input, evidence, or state error for a financing proposal."""


def _value(raw: Any, field_name: str) -> str | float:
    if field_name in TEXT_FIELDS:
        if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 600:
            raise FinancingProposalError("Textvärdet måste vara 1–600 tecken")
        return raw.strip()
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise FinancingProposalError("Belopp eller procentsats måste vara ett ändligt tal")
    value = float(raw)
    if value < 0:
        raise FinancingProposalError("Belopp eller procentsats får inte vara negativ")
    if field_name.endswith("_pct") and value > 1:
        raise FinancingProposalError("Procentsats ska anges som decimal mellan 0 och 1")
    return value


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("proposed_value_json", "previous_value_json"):
        raw = result.get(key)
        if raw is not None:
            result[key.removesuffix("_json")] = json.loads(raw)
        result.pop(key, None)
    return result


def list_proposals(
    connection: sqlite3.Connection, *, status: str | None = "proposed",
    issuer_id: int | None = None, financing_event_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if status is not None and status not in PROPOSAL_STATUSES:
        raise FinancingProposalError("Ogiltig förslagsstatus")
    if not 1 <= int(limit) <= 200:
        raise FinancingProposalError("limit måste vara mellan 1 och 200")
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        where.append("proposal.status=?")
        params.append(status)
    if issuer_id is not None:
        where.append("proposal.issuer_id=?")
        params.append(int(issuer_id))
    if financing_event_id is not None:
        where.append("proposal.financing_event_id=?")
        params.append(int(financing_event_id))
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = connection.execute(
        """SELECT proposal.*, issuer.canonical_name AS issuer_name, document.title AS source_title
           FROM financing_field_proposals proposal
           JOIN issuers issuer ON issuer.id=proposal.issuer_id
           JOIN documents document ON document.id=proposal.source_document_id"""
        + clause + " ORDER BY proposal.created_at DESC, proposal.id DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    counts = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM financing_field_proposals GROUP BY status"
        )
    }
    return {"items": [_row_dict(row) for row in rows], "counts": counts}


def create_proposal(
    connection: sqlite3.Connection, payload: Mapping[str, Any], *, actor: str,
) -> dict[str, Any]:
    try:
        issuer_id = int(payload["issuer_id"])
        event_id = int(payload["financing_event_id"])
        document_id = int(payload["source_document_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FinancingProposalError("Bolag, finansieringshändelse och källdokument krävs") from exc
    field_name = str(payload.get("field_name") or "").strip()
    if field_name not in ALLOWED_FIELDS:
        raise FinancingProposalError("Fältet kan inte ändras genom finansieringsgranskningen")
    value = _value(payload.get("proposed_value"), field_name)
    excerpt = str(payload.get("source_excerpt") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    if len(excerpt) < 12 or len(excerpt) > 2_000:
        raise FinancingProposalError("Källutdraget måste vara 12–2 000 tecken")
    if len(rationale) < 5 or len(rationale) > 1_200:
        raise FinancingProposalError("Motiveringen måste vara 5–1 200 tecken")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise FinancingProposalError("Konfidens måste vara mellan 0 och 1")
    event = connection.execute(
        f"SELECT id, issuer_id, {field_name} AS current_value FROM financing_events WHERE id=? AND issuer_id=?",
        (event_id, issuer_id),
    ).fetchone()
    if event is None:
        raise FinancingProposalError("Finansieringshändelsen tillhör inte bolaget")
    document = connection.execute(
        """SELECT d.id, c.extracted_text FROM documents d
           LEFT JOIN document_contents c ON c.document_id=d.id AND c.status='extracted'
           WHERE d.id=? AND d.issuer_id=? ORDER BY c.id DESC LIMIT 1""",
        (document_id, issuer_id),
    ).fetchone()
    if document is None:
        raise FinancingProposalError("Källdokumentet tillhör inte bolaget")
    source_text = str(document["extracted_text"] or "").casefold()
    if not source_text or excerpt.casefold() not in source_text:
        raise FinancingProposalError("Källutdraget kan inte verifieras i det tolkade dokumentet")
    key_material = json.dumps(
        [issuer_id, event_id, field_name, value, document_id, excerpt],
        ensure_ascii=False, sort_keys=True,
    )
    proposal_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    now = utc_now()
    connection.execute(
        """INSERT INTO financing_field_proposals
           (proposal_key,issuer_id,financing_event_id,field_name,proposed_value_json,
            previous_value_json,source_document_id,source_excerpt,confidence,rationale,
            status,created_by,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?, 'proposed',?,?)
           ON CONFLICT(proposal_key) DO NOTHING""",
        (proposal_key, issuer_id, event_id, field_name, json.dumps(value, ensure_ascii=False),
         json.dumps(event["current_value"], ensure_ascii=False) if event["current_value"] is not None else None,
         document_id, excerpt, float(confidence), rationale, actor, now),
    )
    row = connection.execute(
        "SELECT * FROM financing_field_proposals WHERE proposal_key=?", (proposal_key,)
    ).fetchone()
    assert row is not None
    return _row_dict(row)


def resolve_proposal(
    connection: sqlite3.Connection, proposal_id: int, *, decision: str,
    actor: str, resolution_note: str = "",
) -> dict[str, Any]:
    if decision not in {"apply", "reject", "supersede"}:
        raise FinancingProposalError("Beslut måste vara apply, reject eller supersede")
    row = connection.execute(
        "SELECT * FROM financing_field_proposals WHERE id=?", (int(proposal_id),)
    ).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    if row["status"] != "proposed":
        raise FinancingProposalError("Förslaget har redan behandlats")
    now = utc_now()
    if decision == "apply":
        field_name = str(row["field_name"])
        current = connection.execute(
            f"SELECT {field_name} AS current_value FROM financing_events WHERE id=?",
            (row["financing_event_id"],),
        ).fetchone()
        if current is None:
            raise FinancingProposalError("Finansieringshändelsen saknas")
        if current["current_value"] not in (None, ""):
            raise FinancingProposalError("Fältet har redan ett värde; ersättningsförslag krävs")
        value = json.loads(row["proposed_value_json"])
        connection.execute(
            f"UPDATE financing_events SET {field_name}=?, updated_at=? WHERE id=?",
            (value, now, row["financing_event_id"]),
        )
        connection.execute(
            """INSERT INTO financing_event_enrichment_facts
               (financing_event_id,field_name,value_json,source_document_id,source_excerpt,created_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(financing_event_id,field_name) DO NOTHING""",
            (row["financing_event_id"], field_name, row["proposed_value_json"],
             row["source_document_id"], row["source_excerpt"], now),
        )
        status, applied_at = "applied", now
    else:
        status, applied_at = ("rejected" if decision == "reject" else "superseded"), None
    connection.execute(
        """UPDATE financing_field_proposals
           SET status=?, reviewed_by=?, reviewed_at=?, resolution_note=?, applied_at=?
           WHERE id=?""",
        (status, actor, now, str(resolution_note or "")[:1_200] or None, applied_at, int(proposal_id)),
    )
    resolved = connection.execute("SELECT * FROM financing_field_proposals WHERE id=?", (int(proposal_id),)).fetchone()
    assert resolved is not None
    return _row_dict(resolved)
