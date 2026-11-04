"""Auditable, source-bound corrections layered over immutable extraction facts."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping

from .normalization import utc_now

CORRECTION_TYPES = (
    "fill_field", "replace_field", "clear_field", "change_lifecycle",
    "assign_round", "merge_events", "supersede_duplicate",
    "exclude_cash_effect", "restore_cash_effect",
)
PROPOSAL_STATUSES = ("proposed", "approved", "applied", "rejected", "reverted", "superseded")
TEXT_FIELDS = {"adviser", "issuer_agent", "outcome", "canonical_round_key", "lifecycle_stage", "status", "guarantee_fee_basis"}
NUMBER_FIELDS = {
    "gross_amount", "net_amount", "cash_effect_amount", "issue_price", "shares_issued",
    "final_subscription_amount", "final_subscription_pct", "subscription_with_rights_pct",
    "subscription_without_rights_pct", "subscription_without_guarantees_pct",
    "subscription_commitment_amount", "subscription_commitment_pct",
    "guarantee_commitment_amount", "guarantee_commitment_pct", "guarantee_utilized_amount",
    "guarantee_utilized_pct", "guarantee_take_up_pct_of_issue", "guarantee_fee_amount",
    "guarantee_fee_pct", "guarantee_fee_cash_pct", "guarantee_fee_share_pct",
    "credit_limit", "drawn_amount", "undrawn_commitment", "transaction_cost",
}
ALLOWED_FIELDS = TEXT_FIELDS | NUMBER_FIELDS

FINANCING_CORRECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_correction_proposals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_key TEXT NOT NULL UNIQUE,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
 canonical_round_key TEXT, correction_type TEXT NOT NULL,
 field_name TEXT, previous_value_json TEXT, proposed_value_json TEXT,
 expected_previous_value_json TEXT, source_document_id INTEGER REFERENCES documents(id),
 source_excerpt TEXT, confidence REAL NOT NULL, rationale TEXT NOT NULL,
 impact_preview_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'proposed',
 created_by TEXT NOT NULL, approved_by TEXT, applied_by TEXT, created_at TEXT NOT NULL,
 approved_at TEXT, applied_at TEXT, reverted_at TEXT, resolution_note TEXT,
 CHECK(correction_type IN ('fill_field','replace_field','clear_field','change_lifecycle','assign_round','merge_events','supersede_duplicate','exclude_cash_effect','restore_cash_effect')),
 CHECK(status IN ('proposed','approved','applied','rejected','reverted','superseded'))
);
CREATE INDEX IF NOT EXISTS idx_financing_correction_proposals_queue ON financing_correction_proposals(status, created_at DESC);
CREATE TABLE IF NOT EXISTS financing_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL UNIQUE REFERENCES financing_correction_proposals(id),
 financing_event_id INTEGER NOT NULL REFERENCES financing_events(id), field_name TEXT NOT NULL,
 override_value_json TEXT, source_document_id INTEGER REFERENCES documents(id), source_excerpt TEXT,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_financing_override_active ON financing_overrides(financing_event_id, field_name) WHERE active=1;
CREATE TABLE IF NOT EXISTS financing_round_memberships (
 id INTEGER PRIMARY KEY AUTOINCREMENT, issuer_id INTEGER NOT NULL REFERENCES issuers(id), canonical_round_key TEXT NOT NULL,
 financing_event_id INTEGER NOT NULL REFERENCES financing_events(id), lifecycle_stage TEXT, language TEXT,
 is_primary INTEGER NOT NULL DEFAULT 0, superseded_by_event_id INTEGER REFERENCES financing_events(id),
 cash_effect_included INTEGER NOT NULL DEFAULT 1, proposal_id INTEGER REFERENCES financing_correction_proposals(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(issuer_id, canonical_round_key, financing_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_financing_round_primary ON financing_round_memberships(issuer_id, canonical_round_key) WHERE is_primary=1;
"""


class CorrectionError(ValueError): pass


def _json(value: Any) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True)
def _decode(value: str | None) -> Any: return json.loads(value) if value is not None else None


def _validate_value(field: str, value: Any, correction_type: str) -> Any:
    if correction_type == "clear_field": return None
    if field not in ALLOWED_FIELDS: raise CorrectionError("Fältet är inte tillåtet")
    if field in TEXT_FIELDS:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 600: raise CorrectionError("Ogiltigt textvärde")
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0: raise CorrectionError("Ogiltigt numeriskt värde")
    if field.endswith("_pct") and value > 1: raise CorrectionError("Procentsats ska vara 0–1")
    return float(value)


def _evidence(connection: sqlite3.Connection, event_id: int, document_id: int | None, excerpt: str) -> bool:
    if not document_id or len(excerpt.strip()) < 12: return False
    from .evidence_store import evidence_contains
    if evidence_contains(connection, document_id, excerpt):
        return connection.execute(
            "SELECT 1 FROM financing_event_evidence WHERE financing_event_id=? AND document_id=?",
            (event_id, document_id),
        ).fetchone() is not None
    rows = connection.execute("""SELECT c.extracted_text, e.source_excerpt
        FROM financing_event_evidence e LEFT JOIN document_contents c
          ON c.document_id=e.document_id AND c.status='extracted'
        WHERE e.financing_event_id=? AND e.document_id=?""", (event_id, document_id)).fetchall()
    needle = excerpt.casefold().strip()
    return any(needle in str(row["extracted_text"] or "").casefold() or needle in str(row["source_excerpt"] or "").casefold() for row in rows)


def effective_overrides(connection: sqlite3.Connection, event_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not event_ids: return {}
    slots = ",".join("?" for _ in set(event_ids))
    rows = connection.execute(f"SELECT financing_event_id,field_name,override_value_json FROM financing_overrides WHERE active=1 AND financing_event_id IN ({slots})", tuple(sorted(set(event_ids)))).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows: result.setdefault(int(row["financing_event_id"]), {})[str(row["field_name"])] = _decode(row["override_value_json"])
    return result


def list_corrections(connection: sqlite3.Connection, *, status: str | None = "proposed", limit: int = 50) -> dict[str, Any]:
    if status is not None and status not in PROPOSAL_STATUSES: raise CorrectionError("Ogiltig status")
    if not 1 <= int(limit) <= 200: raise CorrectionError("Ogiltig limit")
    where, args = ("WHERE p.status=?", (status,)) if status else ("", ())
    rows = connection.execute(f"SELECT p.*,i.canonical_name AS issuer_name FROM financing_correction_proposals p JOIN issuers i ON i.id=p.issuer_id {where} ORDER BY p.created_at DESC,p.id DESC LIMIT ?", (*args,int(limit))).fetchall()
    return {"items":[dict(row) for row in rows]}


def round_evidence(connection: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    event=connection.execute("SELECT * FROM financing_events WHERE id=?",(event_id,)).fetchone()
    if event is None: raise KeyError(event_id)
    key=event["canonical_round_key"]
    events=connection.execute("SELECT * FROM financing_events WHERE issuer_id=? AND (? IS NOT NULL AND canonical_round_key=?) ORDER BY announcement_date,effective_date,id",(event["issuer_id"],key,key)).fetchall() if key else [event]
    ids=[int(row["id"]) for row in events]; slots=','.join('?' for _ in ids)
    patches = effective_overrides(connection, ids)
    applied = connection.execute(f"SELECT id,financing_event_id FROM financing_correction_proposals WHERE status='applied' AND financing_event_id IN ({slots})", ids).fetchall()
    applied_by_event: dict[int, list[int]] = {}
    for row in applied: applied_by_event.setdefault(int(row["financing_event_id"]), []).append(int(row["id"]))
    evidence=connection.execute(f"""SELECT e.financing_event_id,e.document_id,e.source_excerpt,e.source_page,
        d.title,d.published_at,d.language,d.canonical_url,
        COALESCE(t.full_text,c.extracted_text) AS extracted_text,
        t.id AS evidence_text_id,t.text_sha256 AS evidence_text_sha256,t.char_count AS evidence_char_count
        FROM financing_event_evidence e JOIN documents d ON d.id=e.document_id
        LEFT JOIN document_contents c ON c.document_id=d.id AND c.status='extracted'
        LEFT JOIN document_evidence_texts t ON t.id=(
            SELECT t2.id FROM document_evidence_texts t2
            WHERE t2.document_id=d.id ORDER BY t2.id DESC LIMIT 1)
        WHERE e.financing_event_id IN ({slots}) ORDER BY d.published_at,e.id""",ids).fetchall()
    entries=[]
    for row in events:
        original=dict(row); effective={**original, **patches.get(int(row["id"]), {})}
        entries.append({"financing_event_id":int(row["id"]),"original_values":original,"effective_values":effective,"applied_correction_ids":applied_by_event.get(int(row["id"]),[])})
    evidence_rows=[]
    for row in evidence:
        item=dict(row); text=str(item.pop("extracted_text") or "")
        excerpt=str(item.get("source_excerpt") or "")
        pos=text.casefold().find(excerpt.casefold()) if excerpt else -1
        item["text_position"] = pos if pos >= 0 else None
        item["text_end_position"] = pos + len(excerpt) if pos >= 0 else None
        item["text_segment"] = text[max(0,pos-240):pos+len(excerpt)+240] if pos >= 0 else excerpt
        item["full_text_available"] = bool(text)
        item["full_text_url"] = f"/api/documents/{item['document_id']}/evidence"
        evidence_rows.append(item)
    return {"canonical_round_key":key,"events":entries,"evidence":evidence_rows}


def correction_detail(connection: sqlite3.Connection, proposal_id: int) -> dict[str, Any]:
    row=connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone()
    if row is None: raise KeyError(proposal_id)
    proposal=dict(row); event=connection.execute("SELECT * FROM financing_events WHERE id=?",(row["financing_event_id"],)).fetchone()
    patch=effective_overrides(connection,[int(row["financing_event_id"])]).get(int(row["financing_event_id"]),{})
    proposal["original_values"]=dict(event) if event else None
    proposal["effective_values"]={**dict(event),**patch} if event else None
    proposal["recalculation_required"]=str(row["field_name"] or "") in {"gross_amount","net_amount","cash_effect_amount","status","canonical_round_key"}
    return proposal


def create_correction(connection: sqlite3.Connection, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
    try: issuer_id, event_id = int(payload["issuer_id"]), int(payload["financing_event_id"])
    except (KeyError, TypeError, ValueError) as exc: raise CorrectionError("issuer_id och financing_event_id krävs") from exc
    kind, field = str(payload.get("correction_type") or ""), str(payload.get("field_name") or "")
    if kind not in CORRECTION_TYPES or (kind not in {"merge_events", "supersede_duplicate"} and field not in ALLOWED_FIELDS): raise CorrectionError("Ogiltig korrigeringstyp eller fält")
    event = connection.execute("SELECT * FROM financing_events WHERE id=? AND issuer_id=?", (event_id, issuer_id)).fetchone()
    if event is None: raise CorrectionError("Finansieringshändelsen tillhör inte bolaget")
    value = _validate_value(field, payload.get("proposed_value"), kind) if field else payload.get("proposed_value")
    expected = payload.get("expected_previous_value", event[field] if field else None)
    if kind in {"replace_field", "clear_field"} and expected != event[field]: raise CorrectionError("expected_previous_value matchar inte ursprungsvärdet")
    document_id, excerpt = payload.get("source_document_id"), str(payload.get("source_excerpt") or "")
    if kind not in {"merge_events", "supersede_duplicate"} and not _evidence(connection, event_id, int(document_id) if document_id else None, excerpt): raise CorrectionError("Källutdraget kan inte verifieras mot lagrad evidens")
    confidence, rationale = payload.get("confidence"), str(payload.get("rationale") or "").strip()
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1 or len(rationale) < 5: raise CorrectionError("Konfidens eller motivering saknas")
    prior = event[field] if field and field in event.keys() else None
    key = hashlib.sha256(_json([issuer_id,event_id,kind,field,value,document_id,excerpt]).encode()).hexdigest()
    now = utc_now(); preview = {"field": field, "previous": prior, "proposed": value, "reported_runway_changed": False, "adjusted_runway_requires_recalculation": field in {"gross_amount","net_amount","cash_effect_amount","status","canonical_round_key"}}
    connection.execute("""INSERT INTO financing_correction_proposals(proposal_key,issuer_id,financing_event_id,canonical_round_key,correction_type,field_name,previous_value_json,proposed_value_json,expected_previous_value_json,source_document_id,source_excerpt,confidence,rationale,impact_preview_json,created_by,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(proposal_key) DO NOTHING""", (key,issuer_id,event_id,payload.get("canonical_round_key"),kind,field,_json(prior),_json(value),_json(expected),document_id,excerpt,confidence,rationale,_json(preview),actor,now))
    row=connection.execute("SELECT * FROM financing_correction_proposals WHERE proposal_key=?",(key,)).fetchone(); return dict(row)


def approve_correction(connection: sqlite3.Connection, proposal_id: int, *, actor: str) -> dict[str, Any]:
    row=connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone()
    if row is None: raise KeyError(proposal_id)
    if row["status"] != "proposed": raise CorrectionError("Förslaget kan inte godkännas i nuvarande status")
    now=utc_now(); connection.execute("UPDATE financing_correction_proposals SET status='approved',approved_by=?,approved_at=? WHERE id=?",(actor,now,proposal_id))
    return dict(connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone())


def apply_correction(connection: sqlite3.Connection, proposal_id: int, *, actor: str) -> dict[str, Any]:
    row=connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone()
    if row is None: raise KeyError(proposal_id)
    if row["status"] != "approved": raise CorrectionError("Endast godkända förslag får tillämpas")
    field=str(row["field_name"] or ""); event=connection.execute("SELECT * FROM financing_events WHERE id=?",(row["financing_event_id"],)).fetchone()
    current = event[field] if event is not None and field in event.keys() else None
    if event is None or (field and _decode(row["expected_previous_value_json"]) != current): raise CorrectionError("Ursprungsvärdet har ändrats; skapa ett nytt förslag")
    if row["correction_type"] not in {"merge_events","supersede_duplicate"} and not _evidence(connection,int(row["financing_event_id"]),row["source_document_id"],str(row["source_excerpt"] or "")): raise CorrectionError("Källutdraget kan inte längre verifieras")
    now=utc_now()
    if field:
        connection.execute("INSERT INTO financing_overrides(proposal_id,financing_event_id,field_name,override_value_json,source_document_id,source_excerpt,created_at) VALUES(?,?,?,?,?,?,?)",(proposal_id,row["financing_event_id"],field,row["proposed_value_json"],row["source_document_id"],row["source_excerpt"],now))
    connection.execute("UPDATE financing_correction_proposals SET status='applied',applied_by=?,applied_at=? WHERE id=?",(actor,now,proposal_id))
    return dict(connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone())


def revert_correction(connection: sqlite3.Connection, proposal_id: int, *, actor: str) -> dict[str, Any]:
    row=connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone()
    if row is None: raise KeyError(proposal_id)
    if row["status"] != "applied": raise CorrectionError("Endast tillämpade förslag kan återställas")
    now=utc_now(); connection.execute("UPDATE financing_overrides SET active=0,reverted_at=? WHERE proposal_id=? AND active=1",(now,proposal_id)); connection.execute("UPDATE financing_correction_proposals SET status='reverted',reverted_at=?,resolution_note=? WHERE id=?",(now,f"Återställd av {actor}",proposal_id))
    return dict(connection.execute("SELECT * FROM financing_correction_proposals WHERE id=?",(proposal_id,)).fetchone())
