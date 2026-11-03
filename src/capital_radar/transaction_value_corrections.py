"""Reviewed transaction read-model corrections; source extraction stays immutable."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .normalization import utc_now
from .evidence_store import evidence_contains, evidence_status


class SourceChronologyError(ValueError):
    """A literal safe code, never source or exception text."""
    code = 'source_chronology_conflict'

    def __init__(self):
        super().__init__(self.code)


def value_proposal_identity(connection, transaction_id, issuer_id, field, expected,
                            proposed, document_id, excerpt, *, stage_evidence=None):
    """Fence an A→B request to the committed field journal, including A→B→A.

    The HTTP idempotency key and immutable request are separate and unchanged.
    Unrelated fields/revisions must not manufacture new correction identities.
    The latest committed override ID also distinguishes a reverted-to-raw A.
    """
    identity = [transaction_id,issuer_id,field,expected,proposed,document_id,excerpt]
    row = connection.execute("""SELECT MAX(id) AS latest,
        MAX(CASE WHEN active=1 THEN correction_id END) AS current
        FROM financing_transaction_value_overrides WHERE transaction_id=? AND field_name=?""",
        (transaction_id,field)).fetchone()
    if row and row['latest'] is not None:
        identity.append({'field_context':[row['current'],row['latest']]})
    from .grouping_consolidation import _rows
    for binding in _rows(connection):
        for part in json.loads(binding['plan_json']):
            if part['transaction_id']!=transaction_id:continue
            inherited=[p['field_correction_ids'][field] for owner,p in part['value_owners'].items()
                       if owner!=transaction_id and field in p['field_correction_ids']]
            if inherited:identity.append({'inherited_field_context':sorted(inherited)})
    if stage_evidence is not None:
        identity.append({'stage_evidence_context':hashlib.sha256(_json(stage_evidence).encode()).hexdigest()})
    return identity


def validate_source_chronology(connection, transaction_id, field, document_id):
    # Narrowly protect an already applied official amount rectification. This
    # does not choose a new value, rewrite historical jobs, or rank unrelated
    # fields and reports. The caller must cite the current corrected source.
    if field not in {'amount','gross_amount','announced_gross_amount','actual_gross_amount'}:
        return
    patch=effective_transaction_overrides(connection,[transaction_id]).get(transaction_id,{})
    correction_id=(patch.get('field_correction_ids') or {}).get(field)
    active = connection.execute("""SELECT c.source_document_id,d.title,d.published_at
        FROM financing_transaction_value_corrections c JOIN documents d ON d.id=c.source_document_id
        WHERE c.id=?""", (correction_id,)).fetchone()
    if (active is None or active['source_document_id']==document_id
            or not re.match(r'^\s*(?:Correction|Rättelse)\s*:',active['title'],re.I)):
        return
    proposed = connection.execute('SELECT published_at FROM documents WHERE id=?',(document_id,)).fetchone()
    try:
        def stamp(value):
            result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
        older=proposed is None or stamp(proposed[0])<stamp(active['published_at'])
    except (ValueError,TypeError):
        older=True
    if older:
        raise SourceChronologyError()

TERM_FIELDS = frozenset({
    'gross_amount','net_amount','announced_gross_amount','announced_net_amount',
    'actual_net_amount','estimated_net_amount','issue_price','shares_issued',
    'final_subscription_amount','final_subscription_pct','subscription_with_rights_pct',
    'subscription_without_rights_pct','subscription_without_guarantees_pct',
    'subscription_commitment_amount','subscription_commitment_pct',
    'guarantee_commitment_amount','guarantee_commitment_pct','guarantee_utilized_amount',
    'guarantee_utilized_pct','guarantee_fee_amount','guarantee_fee_pct',
    'guarantee_fee_cash_pct','guarantee_fee_share_pct','guarantee_fee_basis',
    'transaction_cost','transaction_cost_pct','transaction_cost_basis','net_amount_basis',
    'credit_limit','drawn_amount','undrawn_commitment',
})
# Demand can exceed the offered number of shares. Keep these values as
# fractions (1.58 = 158%), with the existing final-outcome safety ceiling.
# Fees, guarantees and allocations with subscription rights remain <= 1.
OVERSUBSCRIPTION_FIELDS = frozenset({
    'final_subscription_pct', 'subscription_without_rights_pct',
    'subscription_without_guarantees_pct',
})
VALUE_FIELDS = TERM_FIELDS | frozenset({
    "category", "status", "lifecycle_stage", "amount", "amount_basis",
    "currency", "confirmed_cash_effect_amount", "canonical_round_key", "actual_gross_amount",
    "additional_instruments", "effective_stages",
})
RELATION_FIELDS = frozenset({"document_relation", "financing_event_relation"})
TRANSACTION_VALUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_transaction_value_corrections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 proposal_key TEXT NOT NULL UNIQUE,
 transaction_id TEXT NOT NULL,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 field_name TEXT NOT NULL,
 expected_previous_value_json TEXT NOT NULL,
 proposed_value_json TEXT NOT NULL,
 source_document_id INTEGER NOT NULL REFERENCES documents(id),
 source_excerpt TEXT NOT NULL,
 confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
 rationale TEXT NOT NULL,
 original_transaction_json TEXT NOT NULL,
 effective_transaction_json TEXT,
 status TEXT NOT NULL DEFAULT 'proposed'
   CHECK(status IN ('proposed','approved','applied','reverted','failed')),
 created_by TEXT NOT NULL,
 approved_by TEXT,
 applied_by TEXT,
 reverted_by TEXT,
 created_at TEXT NOT NULL,
 approved_at TEXT,
 applied_at TEXT,
 reverted_at TEXT,
 last_apply_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_transaction_value_status
 ON financing_transaction_value_corrections(status,created_at DESC);
CREATE TABLE IF NOT EXISTS financing_transaction_value_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_value_corrections(id),
 transaction_id TEXT NOT NULL,
 field_name TEXT NOT NULL,
 value_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 deactivated_by_correction_id INTEGER REFERENCES financing_transaction_value_corrections(id),
 created_at TEXT NOT NULL,
 reverted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_value_override_active
 ON financing_transaction_value_overrides(transaction_id,field_name) WHERE active=1;
CREATE TABLE IF NOT EXISTS financing_transaction_document_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_value_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 document_id INTEGER NOT NULL REFERENCES documents(id),
 canonical_round_key TEXT NOT NULL,
 category TEXT NOT NULL,
 financing_event_id INTEGER REFERENCES financing_events(id),
 transaction_id TEXT,
 relation_action TEXT NOT NULL DEFAULT 'link',
 active INTEGER NOT NULL DEFAULT 1,
 deactivated_by_correction_id INTEGER REFERENCES financing_transaction_value_corrections(id),
 created_at TEXT NOT NULL,
 reverted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_document_link_active
 ON financing_transaction_document_links(document_id,canonical_round_key,category)
 WHERE active=1;
"""


from .stage_evidence import SCHEMA as STAGE_EVIDENCE_SCHEMA
TRANSACTION_VALUE_SCHEMA += STAGE_EVIDENCE_SCHEMA


class TransactionValueCorrectionError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value(item: Mapping[str, Any], field: str) -> Any:
    effective = item.get("effective_transaction_values") or {}
    if field in effective:
        return effective[field]
    if field == "effective_stages":
        return ((item.get('details') or {}).get('financing_dossier') or {}).get('round_reconciliation', {}).get('stages', [])
    if field in TERM_FIELDS:
        dossier = (item.get('details') or {}).get('financing_dossier') or {}
        aliases = {'guarantee_fee_cash_pct':'cash_fee_pct','guarantee_fee_share_pct':'share_fee_pct','guarantee_fee_basis':'fee_basis'}
        if field in aliases:
            return (dossier.get('guarantee_fee_options') or {}).get(aliases[field])
        return dossier.get(field)
    if field == "confirmed_cash_effect_amount":
        return (item.get("details") or {}).get(field)
    if field == "lifecycle_stage":
        return (item.get("details") or {}).get("lifecycle_stage") or item.get("status")
    if field == "canonical_round_key":
        return (item.get("details") or {}).get("financing_dossier", {}).get(field)
    return item.get(field)


def _evidence(connection: sqlite3.Connection, issuer_id: int, document_id: int, excerpt: str) -> bool:
    document = connection.execute(
        "SELECT issuer_id,title FROM documents WHERE id=?", (document_id,)
    ).fetchone()
    if document is None or int(document["issuer_id"]) != issuer_id:
        return False
    if evidence_contains(connection, document_id, excerpt):
        return True
    texts = [str(document["title"] or "")]
    texts.extend(str(row[0] or "") for row in connection.execute(
        "SELECT extracted_text FROM document_contents WHERE document_id=?", (document_id,)
    ))
    texts.extend(str(row[0] or "") for row in connection.execute(
        "SELECT source_excerpt FROM financing_event_evidence WHERE document_id=?", (document_id,)
    ))
    return any(excerpt in text for text in texts)


def _validate_proposed(field: str, value: Any, *, allow_unknown: bool = True) -> Any:
    from .transaction_feed import CATEGORY_LABELS, STATUS_LABELS

    nullable_numbers = (TERM_FIELDS - {'guarantee_fee_basis', 'transaction_cost_basis', 'net_amount_basis'}) | {'amount', 'actual_gross_amount', 'confirmed_cash_effect_amount'}
    nullable_bases = {'guarantee_fee_basis', 'transaction_cost_basis', 'net_amount_basis'}
    if value is None and allow_unknown and field in nullable_numbers | nullable_bases:
        return None
    if field == "additional_instruments":
        if not isinstance(value, list) or not 1 <= len(value) <= 10:
            raise TransactionValueCorrectionError('Separata instrument kräver 1–10 verifierade instrument')
        required = {'canonical_round_key', 'category', 'status', 'lifecycle_stage', 'amount',
                    'amount_basis', 'currency', 'confirmed_cash_effect_amount', 'source_document_ids'}
        result = []
        keys = set()
        for instrument in value:
            if not isinstance(instrument, dict) or set(instrument) != required:
                raise TransactionValueCorrectionError('Ogiltigt instrumentkontrakt')
            docs = instrument['source_document_ids']
            if not isinstance(docs, list) or len(docs) != 1 or type(docs[0]) is not int or docs[0] < 1:
                raise TransactionValueCorrectionError('Varje nytt instrument kräver ett verifierat källdokument')
            normalized = {k: _validate_proposed(k, v, allow_unknown=False) for k, v in instrument.items() if k != 'source_document_ids'}
            if normalized['confirmed_cash_effect_amount'] != 0:
                raise TransactionValueCorrectionError('Instrumentprojektion får inte registrera mottagen kontantlikvid')
            key = normalized['canonical_round_key'].casefold()
            if key in keys:
                raise TransactionValueCorrectionError('Separata instrument kräver unika round keys')
            keys.add(key)
            result.append({**normalized, 'source_document_ids': docs})
        return result
    if field == "effective_stages":
        if not isinstance(value, list) or not 1 <= len(value) <= 50:
            raise TransactionValueCorrectionError('Effektiva stages krävs')
        from datetime import datetime
        result = []
        for stage in value:
            if not isinstance(stage, dict) or set(stage) != {'lifecycle_stage', 'effective_at', 'source_document_ids', 'reported_values'}:
                raise TransactionValueCorrectionError('Ogiltig stage')
            docs = stage['source_document_ids']
            if not isinstance(docs, list) or len(docs) != 1 or type(docs[0]) is not int or docs[0] < 1:
                raise TransactionValueCorrectionError('Stage kräver ett verifierat dokument')
            if not isinstance(stage['effective_at'], str):
                raise TransactionValueCorrectionError('Stage kräver källdatum')
            datetime.fromisoformat(stage['effective_at'].replace('Z', '+00:00'))
            facts = stage['reported_values']
            allowed = {'gross_amount', 'net_amount', 'transaction_cost', 'subscription_commitment_amount',
                       'guarantee_commitment_amount', 'final_subscription_amount', 'guarantee_utilized_amount'}
            if not isinstance(facts, dict) or not set(facts) <= allowed:
                raise TransactionValueCorrectionError('Ogiltiga stage-fält')
            result.append({**stage, 'lifecycle_stage': _validate_proposed('lifecycle_stage', stage['lifecycle_stage']),
                           'reported_values': {k: _validate_proposed(k, v, allow_unknown=False) for k, v in facts.items()}})
        return result

    if field in TERM_FIELDS and field not in {'guarantee_fee_basis','transaction_cost_basis','net_amount_basis'}:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise TransactionValueCorrectionError('Finansieringsfält kräver ett ändligt tal >= 0')
        if field.endswith('_pct') and value > (100 if field in OVERSUBSCRIPTION_FIELDS else 1):
            raise TransactionValueCorrectionError(
                'Procentfält använder andelar: 0.10 betyder 10 procent. '
                'Teckningsutfall och efterfrågan utan rätt/garantier kan överstiga 1 '
                '(högst 100); övriga andelar får inte överstiga 1.'
            )
        if field=='shares_issued' and int(value)!=value:
            raise TransactionValueCorrectionError('Antal aktier måste vara ett heltal')
        return float(value)
    if field in {"amount", "confirmed_cash_effect_amount", "actual_gross_amount"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise TransactionValueCorrectionError("Beloppet måste vara ett ändligt tal >= 0")
        return float(value)
    if field in RELATION_FIELDS:
        if not isinstance(value, Mapping):
            raise TransactionValueCorrectionError("Dokumentkoppling kräver ett objekt")
        try:
            document_id = int(value.get("document_id"))
        except (TypeError, ValueError) as exc:
            raise TransactionValueCorrectionError("Ogiltigt dokument-ID") from exc
        key = str(value.get("canonical_round_key") or "").strip()
        category = str(value.get("category") or "").strip()
        action = str(value.get("action") or "link").strip()
        if document_id < 1 or not key or len(key) > 180 or category not in CATEGORY_LABELS:
            raise TransactionValueCorrectionError("Dokumentkoppling kräver dokument, round key och kategori")
        if action not in {"link", "copy", "move", "unlink"}:
            raise TransactionValueCorrectionError("Ogiltig dokumentåtgärd")
        event_id = value.get("financing_event_id")
        if field == "financing_event_relation" and event_id is None:
            raise TransactionValueCorrectionError("financing_event_id krävs")
        return {
            "document_id": document_id,
            "canonical_round_key": key,
            "category": category,
            "action": action,
            "financing_event_id": int(event_id) if event_id is not None else None,
            "transaction_id": str(value.get("transaction_id") or "").strip() or None,
        }
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 180:
        raise TransactionValueCorrectionError("Ogiltigt textvärde")
    value = value.strip()
    if field == "category" and value not in CATEGORY_LABELS:
        raise TransactionValueCorrectionError("Okänd finansieringskategori")
    if field == "status" and value not in STATUS_LABELS:
        raise TransactionValueCorrectionError("Okänd transaktionsstatus")
    if field == "currency" and (len(value) != 3 or not value.isalpha()):
        raise TransactionValueCorrectionError("Valuta måste vara ISO-kod")
    if field == "amount_basis" and value not in {
        "announced_amount", "announced_gross", "announced_net",
        "preliminary_outcome", "outcome_amount", "outcome_gross", "outcome_net",
        "maximum_proceeds", "loan_principal", "drawn_loan", "credit_limit",
        "conditional_liquidity", "subscription_commitment", "guarantee_commitment",
    }:
        raise TransactionValueCorrectionError("Okänd beloppsgrund")
    if field == "lifecycle_stage" and value not in {
        "considering", "announced", "terms_published", "subscription_open",
        "preliminary_outcome", "outcome_published", "registered", "settled",
        "cancelled", "conditional",
    }:
        raise TransactionValueCorrectionError("Okänd lifecycle")
    return value


def validate_projection_scope(field, proposed, item, source_document_id, *, connection=None, stage_evidence=None, excerpt=None):
    """Keep structured projections bound to the one archived proposal source."""
    if stage_evidence is not None and field!='effective_stages':raise TransactionValueCorrectionError('stage_evidence_scope')
    if field not in {'additional_instruments', 'effective_stages'}:
        return
    if stage_evidence is not None:
        if field!='effective_stages' or connection is None:raise TransactionValueCorrectionError('stage_evidence_scope')
        from .stage_evidence import verify
        verify(connection,stage_evidence,proposed,item,source_document_id,excerpt)
        return
    if any(part['source_document_ids'] != [source_document_id] for part in proposed):
        raise TransactionValueCorrectionError('Alla projektionsdelar kräver förslagets verifierade källdokument')
    if field == 'additional_instruments':
        parent_key = ((item.get('details') or {}).get('financing_dossier') or {}).get('canonical_round_key')
        if any(part['canonical_round_key'].casefold() == str(parent_key or '').casefold() for part in proposed):
            raise TransactionValueCorrectionError('Separat instrument får inte återanvända huvudrundans identitet')


def additional_instrument_projection_matches(parent_id, proposed, feed):
    """Verify actual cards, including unkeyed raw candidates, without deduping.

    Equal source/category/amount/currency is not proof of legal identity. It is
    an ambiguity that must block verification, rather than silently merging
    cards or counting both a raw candidate and its audited projection.
    """
    children = [card for card in feed if card.get('projection_parent_id') == parent_id
                and card.get('projection_origin') == 'audited_separate_instrument']
    if len(children) != len(proposed):
        return False
    for instrument in proposed:
        matches = [card for card in children
                   if _value(card, 'canonical_round_key') == instrument['canonical_round_key']]
        if len(matches) != 1:
            return False
        child = matches[0]
        if sorted(child['source_document_ids']) != sorted(instrument['source_document_ids']):
            return False
        if any(_value(child, field) != value
               for field, value in instrument.items() if field != 'source_document_ids'):
            return False
        source_ids = set(instrument['source_document_ids'])
        for other in feed:
            if other is child:
                continue
            if (source_ids.intersection(other.get('source_document_ids', []))
                    and _value(other, 'category') == instrument['category']
                    and _value(other, 'currency') == instrument['currency']
                    and _value(other, 'amount') == instrument['amount']):
                return False
    return True


def _raw_transaction_overrides(
    connection: sqlite3.Connection, transaction_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not transaction_ids:
        return {}
    ids = sorted(set(transaction_ids))
    slots = ",".join("?" for _ in ids)
    try:
        rows = connection.execute(
            f"""SELECT o.transaction_id,o.field_name,o.value_json,o.correction_id,c.source_document_id
                FROM financing_transaction_value_overrides o
                JOIN financing_transaction_value_corrections c ON c.id=o.correction_id
                WHERE o.active=1 AND o.transaction_id IN ({slots})""",
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = result.setdefault(str(row["transaction_id"]), {
            "values": {}, "correction_ids": [], "field_correction_ids": {}, "field_source_ids": {},
        })
        item["values"][str(row["field_name"])] = json.loads(row["value_json"])
        item["correction_ids"].append(int(row["correction_id"]))
        item['field_correction_ids'][str(row['field_name'])] = int(row['correction_id'])
        item['field_source_ids'][str(row['field_name'])] = int(row['source_document_id'])
    return result


def effective_transaction_overrides(connection, transaction_ids):
    raw = _raw_transaction_overrides(connection,transaction_ids)
    from .grouping_consolidation import merged_overrides
    return merged_overrides(connection,transaction_ids,raw)


def _relation_current(connection: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            """SELECT canonical_round_key,category,financing_event_id,transaction_id
               FROM financing_transaction_document_links
               WHERE document_id=? AND active=1 ORDER BY id""",
            (document_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def create_transaction_value_correction(
    connection: sqlite3.Connection, payload: Mapping[str, Any], *, actor: str
) -> dict[str, Any]:
    from .transaction_feed import get_transaction_detail

    transaction_id = str(payload.get("transaction_id") or "").strip()
    field = str(payload.get("field_name") or "").strip()
    try:
        issuer_id = int(payload.get("issuer_id"))
        document_id = int(payload.get("source_document_id"))
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise TransactionValueCorrectionError("Issuer, källdokument och confidence krävs") from exc
    if field not in VALUE_FIELDS | RELATION_FIELDS or not transaction_id.startswith("transaction:"):
        raise TransactionValueCorrectionError("Ogiltigt transaktionsfält")
    if not 0 <= confidence <= 1 or not math.isfinite(confidence) or confidence < 0.95:
        raise TransactionValueCorrectionError("Confidence måste vara minst 0,95")
    if "expected_previous_value" not in payload:
        raise TransactionValueCorrectionError("expected_previous_value krävs")
    excerpt = str(payload.get("source_excerpt") or "")
    rationale = str(payload.get("rationale") or "").strip()
    if evidence_status(connection, document_id)["evidence_status"] != "ready":
        raise TransactionValueCorrectionError("Källdokumentet måste vara rehydrerat (evidence_status=ready)")
    if len(excerpt) < 12 or len(excerpt) > 5000 or not evidence_contains(connection, document_id, excerpt):
        raise TransactionValueCorrectionError("Ordagrant källutdrag måste finnas i källdokumentet")
    if len(rationale) < 8 or len(rationale) > 1200:
        raise TransactionValueCorrectionError("Rationale krävs")
    item = get_transaction_detail(connection, transaction_id)
    if int(item["issuer_id"]) != issuer_id:
        raise TransactionValueCorrectionError("Transaktionen tillhör ett annat bolag")
    if field in VALUE_FIELDS and document_id not in item["source_document_ids"]:
        raise TransactionValueCorrectionError("Källdokumentet hör inte till transaktionen")
    validate_source_chronology(connection,transaction_id,field,document_id)
    proposed = _validate_proposed(field, payload.get("proposed_value"))
    validate_projection_scope(field, proposed, item, document_id,connection=connection,stage_evidence=payload.get("stage_evidence"),excerpt=excerpt)
    if field in RELATION_FIELDS:
        relation_doc = int(proposed["document_id"])
        if relation_doc != document_id:
            raise TransactionValueCorrectionError(
                "source_document_id måste vara samma dokument som relationsrättningen"
            )
        source = connection.execute("SELECT issuer_id FROM documents WHERE id=?", (relation_doc,)).fetchone()
        if source is None or int(source["issuer_id"]) != issuer_id:
            raise TransactionValueCorrectionError("Relationsdokumentet tillhör ett annat bolag")
        if proposed["financing_event_id"] is not None:
            event = connection.execute(
                "SELECT issuer_id,financing_type,event_type FROM financing_events WHERE id=?",
                (proposed["financing_event_id"],),
            ).fetchone()
            if event is None or int(event["issuer_id"]) != issuer_id:
                raise TransactionValueCorrectionError("Financing event tillhör ett annat bolag")
            event_category = str(event["financing_type"] or event["event_type"] or "")
            event_category = {
                "loan_draw": "other_debt",
                "loan_commitment": "other_debt",
            }.get(event_category, event_category)
            if event_category != proposed["category"]:
                raise TransactionValueCorrectionError("Financing event har en annan kategori")
        previous = _relation_current(connection, relation_doc)
    else:
        previous = _value(item, field)
    expected = payload["expected_previous_value"]
    if _json(expected) != _json(previous):
        raise TransactionValueCorrectionError("Expected previous value matchar inte effective feed")
    identity = value_proposal_identity(connection,transaction_id,issuer_id,field,expected,proposed,document_id,excerpt,**({"stage_evidence":payload["stage_evidence"]}if "stage_evidence"in payload else{}))
    key = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    now = utc_now()
    connection.execute(
        """INSERT INTO financing_transaction_value_corrections(
               proposal_key,transaction_id,issuer_id,field_name,
               expected_previous_value_json,proposed_value_json,source_document_id,
               source_excerpt,confidence,rationale,original_transaction_json,
               created_by,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(proposal_key) DO NOTHING""",
        (key, transaction_id, issuer_id, field, _json(expected), _json(proposed),
         document_id, excerpt, confidence, rationale, _json(item), actor, now),
    )
    correction=dict(connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE proposal_key=?", (key,)).fetchone())
    from .stage_evidence import save as save_stage_evidence
    save_stage_evidence(connection,correction['id'],payload.get('stage_evidence'))
    return correction


def approve_transaction_value_correction(connection: sqlite3.Connection, correction_id: int, *, actor: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "approved":
        return dict(row)
    if row["status"] != "proposed":
        raise TransactionValueCorrectionError("Endast förslag kan godkännas")
    connection.execute(
        "UPDATE financing_transaction_value_corrections SET status='approved',approved_by=?,approved_at=? WHERE id=?",
        (actor, utc_now(), correction_id),
    )
    return dict(connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone())


def _apply_relation(
    connection: sqlite3.Connection, correction_id: int, row: sqlite3.Row,
    proposed: dict[str, Any], now: str, current: Mapping[str, Any],
) -> None:
    action = proposed["action"]
    document_id = proposed["document_id"]
    if action in {"move", "unlink"}:
        # A source document may legitimately support several instruments or
        # rounds.  Move/unlink only the relation represented by the selected
        # transaction; never deactivate the document's unrelated links.
        source_key = str(
            (current.get("details") or {}).get("financing_dossier", {}).get(
                "canonical_round_key"
            ) or ""
        ).strip()
        clauses = ["document_id=?", "active=1", "category=?"]
        where_parameters: list[Any] = [document_id, current["category"]]
        if source_key:
            clauses.append("canonical_round_key=?")
            where_parameters.append(source_key)
        source_transaction_id = str(current.get("id") or row["transaction_id"])
        clauses.append("(transaction_id=? OR transaction_id IS NULL)")
        where_parameters.append(source_transaction_id)
        connection.execute(
            f"""UPDATE financing_transaction_document_links
                SET active=0,reverted_at=?,deactivated_by_correction_id=?
                WHERE {' AND '.join(clauses)}""",
            [now, correction_id, *where_parameters],
        )
    if action == "copy":
        source_key = str((current.get("details") or {}).get("financing_dossier", {}).get("canonical_round_key") or "").strip()
        if not source_key:
            source_key = f"source_{row['transaction_id'].replace(':', '_')}"
        if source_key.casefold() == proposed["canonical_round_key"].casefold():
            raise TransactionValueCorrectionError("Kopiering kräver en annan mål-runda")
        connection.execute(
            """INSERT OR IGNORE INTO financing_transaction_document_links(
                   correction_id,issuer_id,document_id,canonical_round_key,category,
                   financing_event_id,transaction_id,relation_action,created_at
               ) VALUES(?,?,?,?,?,?,?,'copy',?)""",
            (correction_id, row["issuer_id"], document_id, source_key,
             current["category"], None, current["id"], now),
        )
    connection.execute(
        """INSERT INTO financing_transaction_document_links(
               correction_id,issuer_id,document_id,canonical_round_key,category,
               financing_event_id,transaction_id,relation_action,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (correction_id, row["issuer_id"], document_id,
         proposed["canonical_round_key"], proposed["category"],
         proposed["financing_event_id"], proposed["transaction_id"], action, now),
    )


def apply_transaction_value_correction(
    connection: sqlite3.Connection, correction_id: int, *, actor: str, check_cancelled=None
) -> dict[str, Any]:
    from .transaction_feed import get_transaction_detail, _build_transaction_items
    from .radar_analysis import current_analysis, recalculate_adjusted_runway, unavailable_runway_snapshot

    row = connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "applied":
        return dict(row)
    if row["status"] not in {"approved", "failed"}:
        raise TransactionValueCorrectionError("Endast godkända rättningar får tillämpas")
    field = str(row["field_name"])
    proposed = json.loads(row["proposed_value_json"])
    expected = json.loads(row["expected_previous_value_json"])
    if check_cancelled:
        check_cancelled()
    current = next((item for item in _build_transaction_items(connection, issuer_ids=[int(row["issuer_id"])], check_cancelled=check_cancelled)
                    if item["id"] == row["transaction_id"]), None)
    if current is None:
        raise TransactionValueCorrectionError("Transaktionen saknas")
    from .stage_evidence import load as load_stage_evidence
    validate_projection_scope(field, proposed, current, int(row['source_document_id']),connection=connection,stage_evidence=load_stage_evidence(connection,correction_id),excerpt=row['source_excerpt'])
    validate_source_chronology(connection,row['transaction_id'],field,int(row['source_document_id']))
    previous = _relation_current(connection, proposed["document_id"]) if field in RELATION_FIELDS else _value(current, field)
    if _json(previous) != _json(expected):
        raise TransactionValueCorrectionError("Effective value ändrades efter förslaget")
    before_rows = current_analysis(connection, [int(row["issuer_id"])], check_cancelled=check_cancelled)
    before_snapshot = before_rows[0] if before_rows else unavailable_runway_snapshot(int(row['issuer_id']))
    now = utc_now()
    connection.execute("SAVEPOINT transaction_value_apply")
    try:
        if field in RELATION_FIELDS:
            _apply_relation(connection, correction_id, row, proposed, now, current)
        else:
            connection.execute(
                """UPDATE financing_transaction_value_overrides
                   SET active=0,reverted_at=?,deactivated_by_correction_id=?
                   WHERE transaction_id=? AND field_name=? AND active=1""",
                (now, correction_id, row["transaction_id"], field),
            )
            connection.execute(
                """INSERT INTO financing_transaction_value_overrides(
                       correction_id,transaction_id,field_name,value_json,created_at
                   ) VALUES(?,?,?,?,?)""",
                (correction_id, row["transaction_id"], field, _json(proposed), now),
            )
        rebuilt = [item for item in _build_transaction_items(connection, issuer_ids=[int(row["issuer_id"])], check_cancelled=check_cancelled)
                   if int(item["issuer_id"]) == int(row["issuer_id"])]
        if not rebuilt:
            raise TransactionValueCorrectionError("Ombyggd feed saknar bolagets transaktion")
        if field == 'additional_instruments' and not additional_instrument_projection_matches(
                row['transaction_id'], proposed, rebuilt):
            raise TransactionValueCorrectionError('additional_instrument_projection_ambiguous')
        recalculation = recalculate_adjusted_runway(
            connection, int(row["issuer_id"]),
            idempotency_key=f"transaction-correction:{correction_id}",
            actor=actor, reason=f"Rättning {correction_id}: {row['rationale']}",
            correction_ids=[correction_id],
            before_snapshot=before_snapshot,
            check_cancelled=check_cancelled,
        )
        connection.execute(
            """UPDATE financing_transaction_value_corrections
               SET status='applied',applied_by=?,applied_at=?,effective_transaction_json=?,
                   last_apply_error=NULL WHERE id=?""",
            (actor, now, _json({"feed": rebuilt, "recalculation": recalculation}), correction_id),
        )
        connection.execute("RELEASE SAVEPOINT transaction_value_apply")
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT transaction_value_apply")
        connection.execute("RELEASE SAVEPOINT transaction_value_apply")
        connection.execute(
            """UPDATE financing_transaction_value_corrections
               SET status='failed',last_apply_error=? WHERE id=?""",
            (str(exc)[:500], correction_id),
        )
        return dict(connection.execute(
            "SELECT * FROM financing_transaction_value_corrections WHERE id=?",
            (correction_id,),
        ).fetchone())
    return dict(connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone())


def revert_transaction_value_correction(connection: sqlite3.Connection, correction_id: int, *, actor: str) -> dict[str, Any]:
    from .radar_analysis import current_analysis, recalculate_adjusted_runway
    row = connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "reverted":
        return dict(row)
    if row["status"] != "applied":
        raise TransactionValueCorrectionError("Endast tillämpade rättningar kan återställas")
    before_rows = current_analysis(connection, [int(row["issuer_id"])])
    if not before_rows:
        raise TransactionValueCorrectionError("Bolaget saknar runway-underlag för atomisk återställning")
    before_snapshot = before_rows[0]
    now = utc_now()
    field = str(row["field_name"])
    proposed = json.loads(row["proposed_value_json"])
    if field in RELATION_FIELDS:
        later = connection.execute(
            """SELECT 1 FROM financing_transaction_document_links
               WHERE document_id=? AND active=1 AND correction_id<>?
               LIMIT 1""",
            (int(proposed["document_id"]), correction_id),
        ).fetchone()
    else:
        later = connection.execute(
            """SELECT 1 FROM financing_transaction_value_overrides
               WHERE transaction_id=? AND field_name=? AND active=1
                 AND correction_id<>? LIMIT 1""",
            (row["transaction_id"], field, correction_id),
        ).fetchone()
    if later is not None:
        raise TransactionValueCorrectionError(
            "En senare aktiv rättning måste återställas först"
        )
    connection.execute("SAVEPOINT transaction_value_revert")
    try:
        connection.execute(
            "UPDATE financing_transaction_value_overrides SET active=0,reverted_at=? WHERE correction_id=? AND active=1",
            (now, correction_id),
        )
        connection.execute(
            """UPDATE financing_transaction_value_overrides
               SET active=1,reverted_at=NULL,deactivated_by_correction_id=NULL
               WHERE deactivated_by_correction_id=?""",
            (correction_id,),
        )
        connection.execute(
            "UPDATE financing_transaction_document_links SET active=0,reverted_at=? WHERE correction_id=? AND active=1",
            (now, correction_id),
        )
        connection.execute(
            """UPDATE financing_transaction_document_links
               SET active=1,reverted_at=NULL,deactivated_by_correction_id=NULL
               WHERE deactivated_by_correction_id=?""",
            (correction_id,),
        )
        recalculation = recalculate_adjusted_runway(
            connection, int(row["issuer_id"]),
            idempotency_key=f"transaction-revert:{correction_id}",
            actor=actor, reason=f"Återställ rättning {correction_id}",
            correction_ids=[], before_snapshot=before_snapshot,
        )
        connection.execute(
            """UPDATE financing_transaction_value_corrections
               SET status='reverted',reverted_by=?,reverted_at=? WHERE id=?""",
            (actor, now, correction_id),
        )
        connection.execute("RELEASE SAVEPOINT transaction_value_revert")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT transaction_value_revert")
        connection.execute("RELEASE SAVEPOINT transaction_value_revert")
        raise
    return dict(connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone())


def transaction_value_correction_detail(connection: sqlite3.Connection, correction_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM financing_transaction_value_corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise KeyError(correction_id)
    item = dict(row)
    original_card = json.loads(item["original_transaction_json"])
    item["original_transaction_values"] = (
        original_card.get("original_transaction_values") or original_card
    )
    from .transaction_feed import _build_transaction_items

    original_documents = set(int(value) for value in original_card.get("source_document_ids", []))
    current_cards = [card for card in _build_transaction_items(connection, issuer_ids=[int(item["issuer_id"])])
                     if int(card["issuer_id"]) == int(item["issuer_id"])
                     and original_documents.intersection(
                         int(value) for value in card.get("source_document_ids", [])
                     )]
    item["effective_transaction_values"] = (
        current_cards[0].get("effective_transaction_values", current_cards[0])
        if len(current_cards) == 1 else [
            card.get("effective_transaction_values", card) for card in current_cards
        ]
    )
    item["affected_feed_items"] = current_cards
    item["applied_transaction_correction_ids"] = sorted({
        int(value) for card in current_cards
        for value in card.get("applied_transaction_correction_ids", [])
    } | {
        int(value["correction_id"]) for value in connection.execute(
            """SELECT correction_id FROM financing_transaction_document_links
               WHERE active=1 AND issuer_id=?""", (item["issuer_id"],)
        ).fetchall()
    })
    item["expected_previous_value"] = json.loads(item.pop("expected_previous_value_json"))
    item["proposed_value"] = json.loads(item.pop("proposed_value_json"))
    stored_apply = json.loads(item["effective_transaction_json"]) if item.get("effective_transaction_json") else {}
    from .radar_analysis import current_analysis
    analysis_rows = current_analysis(connection, [int(item["issuer_id"])])
    adjusted = analysis_rows[0] if analysis_rows else None
    recalculation = stored_apply.get("recalculation") if isinstance(stored_apply, dict) else None
    item["projection_readback"] = {
        "original_transaction": item["original_transaction_values"],
        "effective_transaction": item["effective_transaction_values"],
        "feed": current_cards,
        "adjusted_runway": adjusted,
        "recalculation": recalculation,
        "reported_runway_unchanged": (
            recalculation.get("before", {}).get("reported_runway_months")
            == recalculation.get("after", {}).get("reported_runway_months")
            if isinstance(recalculation, dict) else None
        ),
    }
    return item
