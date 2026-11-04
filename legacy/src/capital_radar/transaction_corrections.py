"""Audited overlays for correcting transaction-feed document grouping."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Any, Mapping

from .normalization import utc_now

CORRECTION_TYPES = (
    "split_transaction",
    "move_document",
    "assign_document_to_round",
    "unlink_document_from_transaction",
    "rebuild_transaction",
)

TRANSACTION_CORRECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_transaction_corrections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 proposal_key TEXT NOT NULL UNIQUE,
 correction_type TEXT NOT NULL,
 transaction_id TEXT NOT NULL,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 expected_document_ids_json TEXT NOT NULL,
 additional_document_ids_json TEXT NOT NULL DEFAULT '[]',
 resulting_rounds_json TEXT NOT NULL,
 reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'proposed',
 original_feed_json TEXT NOT NULL,
 applied_feed_json TEXT,
 created_by TEXT NOT NULL,
 approved_by TEXT,
 applied_by TEXT,
 reverted_by TEXT,
 created_at TEXT NOT NULL,
 approved_at TEXT,
 applied_at TEXT,
 reverted_at TEXT,
 CHECK(correction_type IN (
   'split_transaction','move_document','assign_document_to_round',
   'unlink_document_from_transaction','rebuild_transaction'
 )),
 CHECK(status IN ('proposed','approved','applied','reverted'))
);
CREATE TABLE IF NOT EXISTS financing_transaction_document_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 document_id INTEGER NOT NULL REFERENCES documents(id),
 canonical_round_key TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 reverted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_document_override_active
 ON financing_transaction_document_overrides(document_id) WHERE active=1;
CREATE INDEX IF NOT EXISTS idx_transaction_corrections_status
 ON financing_transaction_corrections(status, created_at DESC);
CREATE TABLE IF NOT EXISTS financing_transaction_projection_bindings (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL UNIQUE REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 canonical_round_key TEXT NOT NULL,
 transaction_id TEXT NOT NULL,
 category TEXT NOT NULL,
 document_ids_json TEXT NOT NULL,
 neutral_document_ids_json TEXT NOT NULL,
 neutral_source_fingerprints_json TEXT NOT NULL,
 retired_transaction_ids_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 reverted_at TEXT
);
CREATE TABLE IF NOT EXISTS financing_transaction_grouping_replacements (
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_corrections(id),
 previous_override_id INTEGER NOT NULL REFERENCES financing_transaction_document_overrides(id),
 PRIMARY KEY(correction_id,previous_override_id)
);
"""


from .instrument_grouping import SCHEMA as INSTRUMENT_GROUPING_SCHEMA
from .grouping_consolidation import SCHEMA as CONSOLIDATION_SCHEMA
TRANSACTION_CORRECTIONS_SCHEMA += INSTRUMENT_GROUPING_SCHEMA + CONSOLIDATION_SCHEMA

# Only these literal codes may cross the agent/audit boundary. Unknown messages
# (including strings beginning with a known code) never become diagnostics.
SAFE_GROUPING_ERROR_CODES = frozenset({
    'instrument_grouping_contract', 'instrument_grouping_expected_state_conflict',
    'instrument_grouping_separate_instrument', 'instrument_grouping_retired_value_override',
    'instrument_grouping_existing_assignment', 'instrument_grouping_other_instrument_changed',
    'instrument_grouping_source_changed', 'instrument_grouping_ambiguous',
    'instrument_grouping_dependent_value_override',
    *('grouping_consolidation_'+code for code in (
        'membership_changed','source_changed','ambiguous','owner_changed','field_conflict',
        'expected_state_conflict','separate_instrument','value_source_conflict','structured_values',
        'identity_conflict','existing_binding','incomplete_previous_round','value_provenance_mismatch',
        'dependent_value_override')),
    'grouping_readback_mismatch',
    *('grouping_unit_split_'+code for code in (
        'expected_state_conflict', 'membership_changed', 'existing_assignment',
        'separate_instrument', 'source_changed', 'source_scope', 'identity_conflict')),
    'grouping_projection_binding_incomplete', 'grouping_projection_binding_ambiguous',
    'grouping_projection_binding_link_conflict', 'grouping_projection_binding_target_mismatch',
    'grouping_projection_neutral_source_unavailable',
    'grouping_rebuild_instrument_series_conflict', 'grouping_rebuild_foreign_value_source',
    'grouping_rebuild_retired_value_override', 'grouping_rebuild_separate_instruments',
    'grouping_rebuild_incomplete_card_membership', 'grouping_rebuild_separate_instrument',
    'grouping_rebuild_snapshot_missing', 'grouping_rebuild_expected_state_conflict',
    'grouping_rebuild_incomplete_previous_round',
    'grouping_rebuild_neutral_archive_changed',
    'grouping_split_expected_state_conflict', 'grouping_split_snapshot_missing',
    'grouping_split_value_source_conflict', 'grouping_split_value_identity_conflict',
    'grouping_split_structured_value_override', 'grouping_value_provenance_mismatch',
})


class TransactionCorrectionError(ValueError):
    def __init__(self, message: str) -> None:
        self.error_code = message if message in SAFE_GROUPING_ERROR_CODES else None
        super().__init__(message)


def safe_grouping_error_code(error: Exception) -> str | None:
    """Classify known local guards; never return raw exception content."""
    if isinstance(error, TransactionCorrectionError) or type(error) is ValueError:
        code = str(error)
        return code if code in SAFE_GROUPING_ERROR_CODES else None
    return None


class GroupingReadbackMismatch(TransactionCorrectionError):
    error_code = "grouping_readback_mismatch"

    def __init__(self) -> None:
        super().__init__(self.error_code)


def grouping_projection_readback(
    rounds: list[dict[str, Any]], feed_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare actual financial projections, preserving duplicate round rows.

    Document overlays alone do not prove that the feed merged those documents.
    A dict keyed by round would also conceal two projections of the same round.
    Compare multisets of exact round/document memberships instead.
    """
    expected = [{"canonical_round_key": group["canonical_round_key"],
                 "document_ids": sorted(int(doc) for doc in group["document_ids"])}
                for group in rounds]
    actual = [{
        "canonical_round_key": (item.get("effective_transaction_values") or {}).get("canonical_round_key")
            or (item.get("details") or {}).get("financing_dossier", {}).get("canonical_round_key"),
        "document_ids": sorted(int(doc) for doc in item.get("source_document_ids", [])),
    } for item in feed_items]
    # Keep readback stable when feed presentation order changes.
    actual.sort(key=lambda group: (group["canonical_round_key"] or "",
                                   group["document_ids"]))

    def memberships(groups: list[dict[str, Any]]) -> Counter:
        return Counter((group["canonical_round_key"], tuple(group["document_ids"]))
                       for group in groups)

    matches = memberships(expected) == memberships(actual)
    return {
        "verification_scope": "transaction_grouping",
        "proposed_document_grouping": expected,
        "effective_document_grouping": actual,
        "grouping_matches_applied": matches,
        "current_round_matches_applied": matches,
        "effective_matches_applied": matches,
        "current_round_value": actual,
    }


def reviewed_grouping_readback(
    connection: sqlite3.Connection, correction_id: int | None,
    rounds: list[dict[str, Any]], feed_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify a saved grouping plus later applied, still-active source additions.

    Application continues to use the strict immutable proposal comparison above.
    Readback may include only additions explicitly audited by a later correction
    that retained this round's original membership. Raw heuristic additions,
    duplicate cards, moved/missing original documents and reverted additions do
    not become verified by this allowance.
    """
    scoped = connection.execute('SELECT * FROM financing_transaction_corrections WHERE id=?', (correction_id,)).fetchone()
    if scoped is not None and _decode(scoped['original_feed_json']).get('_instrument_category'):
        from .instrument_grouping import readback
        return readback(connection, scoped, rounds, feed_items)
    consolidated = connection.execute('SELECT * FROM financing_transaction_grouping_consolidations WHERE correction_id=? AND active=1',(correction_id,)).fetchone()
    if consolidated is not None:
        from .grouping_consolidation import source_current
        verified=grouping_projection_readback(rounds,feed_items)
        ok=source_current(connection,consolidated)
        from .transaction_value_corrections import _raw_transaction_overrides
        for part in json.loads(consolidated['plan_json']):
            aliases=[v for v in part['value_owners'] if v!=part['transaction_id']]
            raw=_raw_transaction_overrides(connection,aliases)
            ok=ok and all(raw.get(v,{})==part['value_owners'][v] for v in aliases)
        ok=bool(scoped and scoped['status']=='applied' and ok and verified['grouping_matches_applied'])
        return {**verified,'effective_matches_applied':ok,'current_round_matches_applied':ok,
            'grouping_matches_applied':ok,'expected_current_document_grouping':rounds,
            'audited_extension_correction_ids':[]}
    current = [{"canonical_round_key": part["canonical_round_key"],
                "document_ids": sorted(int(d) for d in part["document_ids"])}
               for part in rounds]
    row = connection.execute(
        "SELECT issuer_id,status FROM financing_transaction_corrections WHERE id=?",
        (correction_id,),
    ).fetchone()
    extension_ids: set[int] = set()
    original_ids = {d for part in current for d in part["document_ids"]}
    assignments = effective_document_rounds(connection, sorted(original_ids))
    originals_match = all(assignments.get(d) == part["canonical_round_key"]
                          for part in current for d in part["document_ids"])
    if row is not None and row["status"] == "applied" and originals_match:
        additions = connection.execute(
            """SELECT o.document_id,o.canonical_round_key,o.correction_id,
                      c.additional_document_ids_json,c.resulting_rounds_json
               FROM financing_transaction_document_overrides o
               JOIN financing_transaction_corrections c ON c.id=o.correction_id
               WHERE o.issuer_id=? AND c.issuer_id=? AND o.active=1
                 AND c.status='applied' AND c.id>?""",
            (row["issuer_id"], row["issuer_id"], correction_id),
        ).fetchall()
        for addition in additions:
            document_id = int(addition["document_id"])
            if document_id in original_ids:
                continue
            if document_id not in _decode(addition["additional_document_ids_json"]):
                continue
            declared = _decode(addition["resulting_rounds_json"])
            for original, expected in zip(rounds, current):
                key = original["canonical_round_key"]
                if addition["canonical_round_key"] != key:
                    continue
                if any(part["canonical_round_key"] == key
                       and document_id in part["document_ids"]
                       and set(original["document_ids"]).issubset(part["document_ids"])
                       for part in declared):
                    expected["document_ids"].append(document_id)
                    expected["document_ids"].sort()
                    extension_ids.add(int(addition["correction_id"]))
    verified = grouping_projection_readback(current, feed_items)
    if row is None or row["status"] != "applied" or not originals_match:
        verified.update(grouping_matches_applied=False, current_round_matches_applied=False,
                        effective_matches_applied=False)
    return {**verified, "expected_current_document_grouping": current,
            "audited_extension_correction_ids": sorted(extension_ids)}


def _decode(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def effective_document_rounds(
    connection: sqlite3.Connection, document_ids: list[int]
) -> dict[int, str]:
    """Return the active document-to-round overlay without changing extraction."""
    if not document_ids:
        return {}
    ids = sorted(set(int(value) for value in document_ids))
    result: dict[int, str] = {}
    try:
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            slots = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT document_id, canonical_round_key
                    FROM financing_transaction_document_overrides
                    WHERE active=1 AND document_id IN ({slots})""",
                batch,
            ).fetchall()
            result.update({
                int(row["document_id"]): str(row["canonical_round_key"])
                for row in rows
            })
    except sqlite3.OperationalError:
        return {}
    return result


def effective_document_round_links(
    connection: sqlite3.Connection, document_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Project reviewed many-to-many document/round links into the feed."""
    if not document_ids:
        return {}
    ids = sorted(set(int(value) for value in document_ids))
    result: dict[int, list[dict[str, Any]]] = {}
    try:
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            slots = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT document_id,canonical_round_key,category,financing_event_id,
                           transaction_id,relation_action,correction_id
                    FROM financing_transaction_document_links
                    WHERE active=1 AND document_id IN ({slots}) ORDER BY id""",
                batch,
            ).fetchall()
            for row in rows:
                result.setdefault(int(row["document_id"]), []).append(dict(row))
    except sqlite3.OperationalError:
        return {}
    return result


def _neutral_source_fingerprint(connection: sqlite3.Connection, document_id: int) -> str:
    """Pin the exact effective text/title premise; store only its digest."""
    doc = connection.execute('SELECT title,profile_category,published_at,language FROM documents WHERE id=?', (document_id,)).fetchone()
    content = connection.execute("""SELECT extracted_text FROM document_contents WHERE document_id=?
        AND status IN ('extracted','too_short') AND extracted_text IS NOT NULL
        ORDER BY (attachment_id IS NULL) DESC,quality_score DESC,id DESC LIMIT 1""", (document_id,)).fetchone()
    if content is None:
        content = connection.execute('SELECT substr(full_text,1,120000) FROM document_evidence_texts WHERE document_id=? ORDER BY id DESC LIMIT 1', (document_id,)).fetchone()
    if doc is None or content is None:
        raise TransactionCorrectionError('grouping_projection_neutral_source_unavailable')
    evidence = [dict(row) for row in connection.execute(
        'SELECT * FROM financing_event_evidence WHERE document_id=? ORDER BY id', (document_id,))]
    event_ids = sorted({int(row['financing_event_id']) for row in evidence})
    events, overrides = [], {}
    if event_ids:
        slots = ','.join('?' for _ in event_ids)
        events = [dict(row) for row in connection.execute(
            f'SELECT * FROM financing_events WHERE id IN ({slots}) ORDER BY id', event_ids)]
        from .financing_corrections import effective_overrides
        overrides = effective_overrides(connection, event_ids)
    # Existing legacy associations are preserved, not treated as newly parsed
    # finance facts. Any later evidence/event/overlay change invalidates this
    # reviewed premise, including a classification added to a legacy event.
    return hashlib.sha256(json.dumps(
        ['neutral-source-v2',list(doc),str(content[0]),evidence,events,overrides],
        ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _legacy_neutral_event_ids(connection, document_ids, issuer_id):
    """Keep unclassified legacy associations without suppressing real instruments.

    This is used only after the explicit neutral memorandum/card checks. A raw
    generic association is not an independently classified financial instrument.
    Its original rows stay intact and are pinned in the binding fingerprint.
    """
    slots = ','.join('?' for _ in document_ids)
    events = connection.execute(f'''SELECT DISTINCT e.* FROM financing_events e
        JOIN financing_event_evidence v ON v.financing_event_id=e.id
        WHERE v.document_id IN ({slots}) ORDER BY e.id''', document_ids).fetchall()
    from .transaction_feed import _SERIES, _RX
    for event in events:
        if (int(event['issuer_id']) != int(issuer_id) or event['financing_type']
                or event['event_type'] not in {'other','share_issue','rights_issue'}
                or any(pattern.search(str(event['description'] or '')) for kind,pattern in _RX.items()
                       if kind not in {'rights_issue','other_equity'})
                or _SERIES.search(str(event['description'] or ''))):
            raise TransactionCorrectionError('grouping_rebuild_separate_instrument')
    return [int(event['id']) for event in events]


def _neutral_archive_hash(connection, document_id, evidence_id=None):
    """Hash the full immutable archive, independently of the display text cap."""
    row = connection.execute('''SELECT full_text FROM document_evidence_texts
        WHERE document_id=?''' + (' AND id=?' if evidence_id is not None else '') +
        ' ORDER BY id DESC LIMIT 1',
        (document_id,evidence_id) if evidence_id is not None else (document_id,)).fetchone()
    return hashlib.sha256(str(row[0]).encode()).hexdigest() if row is not None else None


def _neutral_job_archive_current(connection, correction_id, document_id):
    """Keep old binding fingerprints usable while checking their archived proof."""
    job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=? ORDER BY id LIMIT 1',
                             (correction_id,)).fetchone()
    if job is None:
        return None
    segments = json.loads(job[0])['payload'].get('evidence_segments',[])
    proofs = [part['evidence_id'] for part in segments if part['document_id']==document_id]
    current = _neutral_archive_hash(connection,document_id)
    return bool(proofs and current is not None and all(
        _neutral_archive_hash(connection,document_id,evidence_id)==current for evidence_id in proofs))


def effective_projection_bindings(connection: sqlite3.Connection, document_rounds: dict[int, str]) -> list[dict[str, Any]]:
    """Read apply-owned identities only while their complete reviewed scope survives.

    Old bindings remain auditable when a later rebuild replaces their key. A
    reversal of that later rebuild restores the original binding automatically.
    Partial identity changes and overlapping ownership fail closed instead of
    choosing one row by dictionary overwrite. A changed neutral premise stops
    suppressing that source, making grouping readback fail without stranding
    the publisher or the audited revert path.
    """
    if not document_rounds:
        return []
    try:
        rows = connection.execute("""SELECT b.* FROM financing_transaction_projection_bindings b
            JOIN financing_transaction_corrections c ON c.id=b.correction_id
            WHERE b.active=1 AND c.status IN ('approved','applied') ORDER BY b.id""").fetchall()
    except sqlite3.OperationalError as error:
        if 'no such table' in str(error):
            return []
        raise
    result, claimed = [], set()
    for row in rows:
        part = dict(row)
        ids = json.loads(part['document_ids_json'])
        # A scoped issuer build does not load other issuers' assignments.
        if not set(ids).intersection(document_rounds):
            continue
        matches = [document_rounds.get(doc) == part['canonical_round_key'] for doc in ids]
        if not any(matches):
            continue
        if not all(matches):
            raise TransactionCorrectionError('grouping_projection_binding_incomplete')
        neutral = json.loads(part['neutral_document_ids_json'])
        fingerprints = json.loads(part['neutral_source_fingerprints_json'])
        changed_neutral = []
        for doc in neutral:
            try:
                current_fingerprint = _neutral_source_fingerprint(connection, doc)
            except TransactionCorrectionError:
                current_fingerprint = None
            saved = fingerprints.get(str(doc))
            saved_fingerprint = saved.get('fingerprint') if isinstance(saved,dict) else saved
            archive_proof = _neutral_job_archive_current(connection,part['correction_id'],doc)
            has_archive_fingerprint = isinstance(saved,dict) and 'archive_text_sha256' in saved
            if (saved_fingerprint != current_fingerprint
                    or (isinstance(saved,dict) and 'archive_text_sha256' in saved
                        and saved['archive_text_sha256'] != _neutral_archive_hash(connection,doc))
                    or archive_proof is False or (archive_proof is None and not has_archive_fingerprint)
                    or connection.execute('SELECT 1 FROM financing_transaction_document_links WHERE document_id=? AND active=1 LIMIT 1',(doc,)).fetchone()):
                changed_neutral.append(doc)
        if claimed.intersection(ids):
            raise TransactionCorrectionError('grouping_projection_binding_ambiguous')
        claimed.update(ids)
        part['document_ids'] = ids
        part['neutral_document_ids'] = neutral
        part['changed_neutral_document_ids'] = changed_neutral
        part['legacy_neutral_financing_event_ids'] = sorted({event_id
            for saved in fingerprints.values() if isinstance(saved,dict)
            for event_id in saved.get('legacy_financing_event_ids',[])})
        part['retired_transaction_ids'] = json.loads(part['retired_transaction_ids_json'])
        result.append(part)
    return result


def rebuild_projection_guard_diagnostics(connection, row, rounds, cards, affected):
    """Inspect only current prerequisites, without applying or claiming success.

    The caller must first scope the correction to the requesting job/actor.
    No source text, monetary values, raw classifications or exception messages
    leave this helper. Counts use distinct event IDs, not language evidence rows.
    """
    from .transaction_value_corrections import effective_transaction_overrides, TERM_FIELDS
    target = next((card for card in cards if card['id'] == row['transaction_id']), None)
    result = {'observation_scope': 'current_prerequisites_not_historical_failure_cause',
              'read_only': True, 'current_rebuild_projection_guard_passed': None,
              'current_validation_code': 'not_evaluated',
              'complete_apply_readiness_verified': False, 'neutral_candidates': []}
    if row['correction_type'] != 'rebuild_transaction' or len(rounds) != 1:
        result['current_validation_code'] = 'not_applicable'
        return result
    if target is None:
        result['current_validation_code'] = 'transaction_not_present'
        return result
    category = target['category']
    financial_docs = sorted({int(doc) for card in cards if card['category'] == category
                             for doc in card['source_document_ids']})
    financial_keys = {str((card.get('effective_transaction_values') or {}).get('canonical_round_key'))
                      for card in cards if card['category'] == category
                      and (card.get('effective_transaction_values') or {}).get('canonical_round_key')}
    requested_keys = {part['canonical_round_key'] for part in rounds}
    target_docs = set(int(doc) for doc in target['source_document_ids'])
    retired = [card['id'] for card in cards if card['id'] != row['transaction_id']]
    patches = effective_transaction_overrides(connection, retired)
    result['retired_active_value_field_count'] = sum(len(part.get('values') or {}) for part in patches.values())
    for card in cards:
        if card['category'] == category:
            continue
        ids = sorted(int(doc) for doc in card['source_document_ids'])
        slots = ','.join('?' for _ in ids)
        events = connection.execute(f"""SELECT DISTINCT e.id,e.issuer_id,e.financing_type,e.event_type,
            e.canonical_round_key FROM financing_events e JOIN financing_event_evidence v ON v.financing_event_id=e.id
            WHERE v.document_id IN ({slots}) ORDER BY e.id""", ids).fetchall()
        same_category, other_category, unknown_category, shared, shared_target, key_matches, requested_matches = [], [], [], [], [], [], []
        for event in events:
            event_id = int(event['id'])
            value = event['financing_type'] or (event['event_type'] if event['event_type'] in {'rights_issue','directed_issue','unit_issue','warrant_exercise','convertible','bridge_loan','shareholder_loan','credit_facility','other_equity','other_debt'} else None)
            (same_category if value == category else other_category if value else unknown_category).append(event_id)
            linked_docs = {int(value[0]) for value in connection.execute(
                'SELECT document_id FROM financing_event_evidence WHERE financing_event_id=?', (event_id,))}
            if linked_docs.intersection(financial_docs):
                shared.append(event_id)
            if linked_docs.intersection(target_docs):
                shared_target.append(event_id)
            if event['canonical_round_key'] in financial_keys:
                key_matches.append(event_id)
            if event['canonical_round_key'] in requested_keys:
                requested_matches.append(event_id)
        values = card.get('effective_transaction_values') or {}
        dossier = (card.get('details') or {}).get('financing_dossier') or {}
        material = sorted(field for field in TERM_FIELDS if values.get(field) is not None or dossier.get(field) is not None)
        result['neutral_candidates'].append({
            'document_ids': ids, 'financing_event_count': len(events),
            'financing_event_ids': [int(event['id']) for event in events],
            'event_issuer_matches_job_count': sum(int(event['issuer_id']) == int(row['issuer_id']) for event in events),
            'event_category_matches_target_count': len(same_category),
            'event_category_other_count': len(other_category), 'event_category_unspecified_count': len(unknown_category),
            'legacy_generic_event_count': sum(not event['financing_type'] and event['event_type'] in {'other','share_issue'} for event in events),
            'legacy_parent_category_event_count': sum(not event['financing_type'] and event['event_type']==category for event in events),
            'events_shared_with_other_financial_sources_count': len(shared),
            'events_shared_with_other_financial_sources_ids': shared,
            'events_shared_with_target_sources_count': len(shared_target),
            'events_matching_current_financial_round_key_count': len(key_matches),
            'events_matching_requested_round_key_count': len(requested_matches),
            'material_value_fields': material,
            'effective_stages_present': bool(values.get('effective_stages')),
            'additional_instruments_present': bool(values.get('additional_instruments')),
        })
    try:
        binding = _prepare_rebuild_binding(connection, row, rounds, cards, affected)
    except TransactionCorrectionError as error:
        code = safe_grouping_error_code(error)
        result['current_rebuild_projection_guard_passed'] = False if code else None
        result['current_validation_code'] = code or 'projection_guard_unclassified'
    else:
        result['current_rebuild_projection_guard_passed'] = binding is not None
        result['current_validation_code'] = 'projection_guard_passed' if binding is not None else 'not_applicable'
    return result


def _prepare_rebuild_binding(connection, row, rounds, cards, affected):
    """A narrow, explicit one-rights-round rebuild; no generic category merge.

    A neutral memorandum clarification is source membership, not a new finance
    candidate. Separate securities and values on retired identities are refused.
    """
    if row['correction_type'] != 'rebuild_transaction' or len(rounds) != 1:
        return None
    from .transaction_feed import _SERIES
    securities = {re.sub(r'\s', '', match.group(1) or match.group()).upper()
                  for card in cards if card['category'] in {'warrant_exercise','convertible'}
                  for entry in card.get('history', []) for match in _SERIES.finditer(entry['title'])}
    if len(securities) > 1:
        raise TransactionCorrectionError('grouping_rebuild_instrument_series_conflict')
    target = next(card for card in cards if card['id'] == row['transaction_id'])
    if target['category'] != 'rights_issue':
        return None
    from .transaction_value_corrections import effective_transaction_overrides, TERM_FIELDS
    ids = [card['id'] for card in cards]
    patches = effective_transaction_overrides(connection, ids)
    retired = [value for value in ids if value != row['transaction_id']]
    if any(int(doc) not in set(affected) for part in patches.values() for doc in part.get('field_source_ids', {}).values()):
        raise TransactionCorrectionError('grouping_rebuild_foreign_value_source')
    if any(patches.get(value, {}).get('values') for value in retired):
        raise TransactionCorrectionError('grouping_rebuild_retired_value_override')
    if (patches.get(row['transaction_id'], {}).get('values') or {}).get('additional_instruments'):
        raise TransactionCorrectionError('grouping_rebuild_separate_instruments')
    neutral = []
    membership = Counter(doc for card in cards for doc in card['source_document_ids'])
    if set(membership) != set(affected) or any(count != 1 for count in membership.values()):
        raise TransactionCorrectionError('grouping_rebuild_incomplete_card_membership')
    for card in cards:
        if card['category'] == target['category']:
            continue
        # Deliberately narrow: a financing headline without a value can still
        # describe a separate legal security. It must not be swallowed here.
        titles = [entry['title'] for entry in card.get('history', [])]
        neutral_title = all(re.search(r'\b(?:clarif\w*|förtydlig\w*)\b', title, re.I)
                            and re.search(r'\b(?:memorandum|prospekt)\b', title, re.I)
                            for title in titles)
        dossier = (card.get('details') or {}).get('financing_dossier') or {}
        values = card.get('effective_transaction_values') or {}
        financial_values = {field: value for field, value in values.items()
                            if field not in {'category','status','lifecycle_stage','canonical_round_key',
                                             'confirmed_cash_effect_amount','effective_stages','additional_instruments'}}
        if (card['category'] != 'other_equity' or not titles or not neutral_title
                or card.get('amount') is not None
                or values.get('confirmed_cash_effect_amount') not in (None, 0)
                or (card.get('details') or {}).get('confirmed_cash_effect_amount') not in (None, 0)
                or any(value is not None for value in financial_values.values())
                or any(dossier.get(field) is not None for field in TERM_FIELDS)
                or values.get('effective_stages') or values.get('additional_instruments')
                or (dossier.get('round_reconciliation') or {}).get('stages')):
            raise TransactionCorrectionError('grouping_rebuild_separate_instrument')
        _legacy_neutral_event_ids(connection,card['source_document_ids'],row['issuer_id'])
        if any(_neutral_job_archive_current(connection,row['id'],int(doc)) is False for doc in card['source_document_ids']):
            raise TransactionCorrectionError('grouping_rebuild_neutral_archive_changed')
        neutral.extend(card['source_document_ids'])
    # The resulting key is authoritative, but this operation never changes the
    # financing category or imports neutral text into amount/lifecycle parsing.
    return {'canonical_round_key': rounds[0]['canonical_round_key'],
            'transaction_id': row['transaction_id'], 'category': target['category'],
            'document_ids': sorted(affected), 'neutral_document_ids': sorted(neutral),
            'retired_transaction_ids': retired,
            'neutral_source_fingerprints': {str(doc): {
                'fingerprint':_neutral_source_fingerprint(connection,doc),
                'archive_text_sha256':_neutral_archive_hash(connection,doc),
                'legacy_financing_event_ids':_legacy_neutral_event_ids(connection,[doc],row['issuer_id'])}
                for doc in neutral}}


def _active_value_snapshot(connection, cards):
    """Keep all affected active correction ownership, including explicit nulls.

    Group membership alone cannot prove that source-bound values survived an
    identity change. This snapshot is read inside the grouping savepoint and
    checked against the rebuilt projection before any runway recalculation.
    """
    from .transaction_value_corrections import effective_transaction_overrides
    patches = effective_transaction_overrides(connection, [card['id'] for card in cards])
    if any(count != 1 and patches.get(owner_id, {}).get('values')
           for owner_id, count in Counter(card['id'] for card in cards).items()):
        raise TransactionCorrectionError('grouping_value_provenance_mismatch')
    return {card['id']: {'category': card['category'], **patches[card['id']]}
            for card in cards if patches.get(card['id'], {}).get('values')}


def _prepare_split_binding(connection, row, rounds, cards, affected, value_snapshot):
    """Preserve one source-owned scalar identity across an explicit split.

    A lower document ID can belong to the separated compensation instrument.
    The surviving rights round may retain its old ID only when every active
    scalar source belongs to one and the same requested round. Cross-round or
    structured ownership is deliberately unsupported, rather than migrated or
    silently discarded. The ordinary projection/category and provenance guards
    still validate the resulting cards inside the apply savepoint.
    """
    if row['correction_type'] != 'split_transaction':
        return None
    expected_inventory = _decode(row['original_feed_json']).get('_expected_grouping_inventory')
    if expected_inventory is None:
        job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?',
                                 (row['id'],)).fetchone()
        if job:
            expected_inventory = json.loads(job[0])['payload'].get('expected_current_grouping')
    if expected_inventory is None:
        raise TransactionCorrectionError('grouping_split_snapshot_missing')
    def inventory(parts):
        return Counter((part['transaction_id'], part['canonical_round_key'], tuple(sorted(part['document_ids'])))
                       for part in parts)
    if inventory(_grouping_inventory(cards, set(affected))) != inventory(expected_inventory):
        raise TransactionCorrectionError('grouping_split_expected_state_conflict')
    if not value_snapshot:
        return None
    # Other legal instruments may already own values (e.g. the currency of
    # the compensation card). Their identities are not rebound or merged.
    # The postprojection guard requires every one of them to survive exactly;
    # an unsupported second moving owner therefore fails inside the savepoint.
    owner_id = row['transaction_id']
    patch = value_snapshot.get(owner_id)
    if patch is None:
        return None
    values, sources = patch['values'], patch.get('field_source_ids') or {}
    if set(values) != set(sources) or not sources:
        raise TransactionCorrectionError('grouping_split_value_source_conflict')
    source_ids = {int(doc) for doc in sources.values()}
    if not source_ids.issubset(set(affected)):
        raise TransactionCorrectionError('grouping_split_value_source_conflict')
    candidates = [part for part in rounds if source_ids.issubset(set(part['document_ids']))]
    if len(candidates) != 1:
        raise TransactionCorrectionError('grouping_split_value_source_conflict')
    target = candidates[0]
    if (values.get('additional_instruments') or values.get('effective_stages')):
        raise TransactionCorrectionError('grouping_split_structured_value_override')
    if (('canonical_round_key' in values and values['canonical_round_key'] != target['canonical_round_key'])
            or ('category' in values and values['category'] != patch['category'])):
        raise TransactionCorrectionError('grouping_split_value_identity_conflict')
    return {'canonical_round_key': target['canonical_round_key'], 'transaction_id': owner_id,
            'category': patch['category'], 'document_ids': sorted(target['document_ids']),
            'neutral_document_ids': [], 'neutral_source_fingerprints': {},
            'retired_transaction_ids': []}


def _assert_value_provenance_preserved(value_snapshot, rebuilt):
    """Never hide a lost, duplicated, moved or numerically replaced overlay."""
    from .transaction_value_corrections import _value
    for owner_id, patch in value_snapshot.items():
        owners = [card for card in rebuilt if card['id'] == owner_id]
        if len(owners) != 1 or owners[0]['category'] != patch['category']:
            raise TransactionCorrectionError('grouping_value_provenance_mismatch')
        owner = owners[0]
        member_ids = {int(doc) for doc in owner['source_document_ids']}
        if set(patch['values']) != set(patch.get('field_source_ids') or {}):
            raise TransactionCorrectionError('grouping_value_provenance_mismatch')
        for field, value in patch['values'].items():
            correction_id = patch['field_correction_ids'].get(field)
            holders = [card for card in rebuilt if correction_id in card.get('applied_transaction_correction_ids', [])]
            if (len(holders) != 1 or holders[0]['id'] != owner_id
                    or field not in owner.get('effective_corrected_fields', [])
                    or int(patch['field_source_ids'][field]) not in member_ids
                    or _value(owner, field) != value):
                raise TransactionCorrectionError('grouping_value_provenance_mismatch')


def _normalise_rounds(
    value: Any, expected: list[int], additional: list[int] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise TransactionCorrectionError("resulting_rounds krävs")
    normalised: list[dict[str, Any]] = []
    assigned: list[int] = []
    keys: set[str] = set()
    for group in value:
        if not isinstance(group, Mapping):
            raise TransactionCorrectionError("Varje resultatgrupp måste vara ett objekt")
        key = str(group.get("canonical_round_key") or "").strip()
        try:
            documents = sorted({int(item) for item in group.get("document_ids") or []})
        except (TypeError, ValueError) as exc:
            raise TransactionCorrectionError("Ogiltigt dokument-ID") from exc
        if not key or len(key) > 180 or not documents:
            raise TransactionCorrectionError("Varje resultatgrupp kräver round key och dokument")
        if key.casefold() in keys:
            raise TransactionCorrectionError("Canonical round key måste vara unik i resultatet")
        keys.add(key.casefold())
        assigned.extend(documents)
        normalised.append({"canonical_round_key": key, "document_ids": documents})
    allowed = sorted(set(expected) | set(additional or []))
    if sorted(assigned) != allowed or len(assigned) != len(set(assigned)):
        raise TransactionCorrectionError("Varje källdokument ska finnas exakt en gång")
    return normalised


def _affected_feed_items(
    connection: sqlite3.Connection, document_ids: list[int], *, issuer_id=None, check_cancelled=None
) -> list[dict[str, Any]]:
    from .transaction_feed import _build_transaction_items

    expected = set(document_ids)
    return [
        item for item in _build_transaction_items(connection, issuer_ids=[issuer_id] if issuer_id else None, check_cancelled=check_cancelled)
        if expected & {int(value) for value in item.get("source_document_ids", [])}
    ]


def _grouping_inventory(cards, affected):
    return [{'transaction_id': card['id'],
             'canonical_round_key': (card.get('effective_transaction_values') or {}).get('canonical_round_key'),
             'document_ids': sorted(set(card['source_document_ids']).intersection(affected))}
            for card in cards if set(card['source_document_ids']).intersection(affected)]


def create_transaction_correction(
    connection: sqlite3.Connection, payload: Mapping[str, Any], *, actor: str, check_cancelled=None
) -> dict[str, Any]:
    kind = str(payload.get("correction_type") or "")
    transaction_id = str(payload.get("transaction_id") or "")
    try:
        issuer_id = int(payload.get("issuer_id") or 0)
        expected = sorted({
            int(value) for value in payload.get("source_transaction_document_ids") or []
        })
        supplied_additional = payload.get("additional_document_ids")
        if supplied_additional is None and payload.get("document_id") is not None:
            supplied_additional = [payload.get("document_id")]
        additional = sorted({int(value) for value in supplied_additional or []})
    except (TypeError, ValueError) as exc:
        raise TransactionCorrectionError("Ogiltiga dokument- eller issuer-ID") from exc
    if kind not in CORRECTION_TYPES or not transaction_id.startswith("transaction:") or issuer_id < 1:
        raise TransactionCorrectionError("Ogiltig rättningstyp, transaktion eller issuer")
    if not expected:
        raise TransactionCorrectionError("Expected dokument krävs")
    if set(expected) & set(additional):
        raise TransactionCorrectionError("Ett tillagt dokument finns redan i transaktionen")
    rounds_input = payload.get("resulting_rounds")
    canonical_round_key = str(payload.get("canonical_round_key") or "").strip()
    if additional and rounds_input is None:
        if not canonical_round_key:
            raise TransactionCorrectionError("canonical_round_key krävs för tillagt dokument")
        rounds_input = [{
            "canonical_round_key": canonical_round_key,
            "document_ids": sorted(expected + additional),
        }]
    rounds = _normalise_rounds(rounds_input, expected, additional)

    from .transaction_feed import _build_transaction_items

    current = next((i for i in _build_transaction_items(connection,issuer_ids=[issuer_id],check_cancelled=check_cancelled) if i['id']==transaction_id),None)
    if current is None:
        raise TransactionCorrectionError('Transaktionen saknas')
    if (
        int(current["issuer_id"]) != issuer_id
        or sorted(int(value) for value in current["source_document_ids"]) != expected
    ):
        raise TransactionCorrectionError("Expected state matchar inte aktuell feedpost")
    if additional:
        slots = ",".join("?" for _ in additional)
        documents = connection.execute(
            f"SELECT id,issuer_id FROM documents WHERE id IN ({slots})", additional
        ).fetchall()
        if ({int(row["id"]) for row in documents} != set(additional)
                or any(int(row["issuer_id"]) != issuer_id for row in documents)):
            raise TransactionCorrectionError("Tillagda dokument måste vara lagrade för samma issuer")
    instrument_category = payload.get('instrument_category')
    if instrument_category is not None and (instrument_category != 'warrant_exercise' or kind != 'rebuild_transaction' or len(rounds) != 1):
        raise TransactionCorrectionError('instrument_grouping_contract')
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < 5 or len(reason) > 1200:
        raise TransactionCorrectionError("Motivering krävs")
    proposal_key = hashlib.sha256(json.dumps(
        [kind, transaction_id, issuer_id, expected, additional, rounds] + ([instrument_category] if instrument_category else []),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    now = utc_now()
    current = {**current, **({'_instrument_category': instrument_category} if instrument_category else {}), '_expected_grouping_inventory': _grouping_inventory(
        _affected_feed_items(connection, expected + additional, issuer_id=issuer_id, check_cancelled=check_cancelled),
        set(expected + additional))}
    connection.execute(
        """INSERT INTO financing_transaction_corrections(
               proposal_key, correction_type, transaction_id, issuer_id,
               expected_document_ids_json, additional_document_ids_json,
               resulting_rounds_json, reason,
               original_feed_json, created_by, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(proposal_key) DO NOTHING""",
        (
            proposal_key, kind, transaction_id, issuer_id,
            json.dumps(expected), json.dumps(additional),
            json.dumps(rounds, ensure_ascii=False), reason,
            json.dumps(current, ensure_ascii=False), actor, now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE proposal_key=?",
        (proposal_key,),
    ).fetchone()
    return dict(row)


def approve_transaction_correction(
    connection: sqlite3.Connection, correction_id: int, *, actor: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "approved":
        return dict(row)
    if row["status"] != "proposed":
        raise TransactionCorrectionError("Rättningen kan inte godkännas")
    connection.execute(
        """UPDATE financing_transaction_corrections
           SET status='approved', approved_by=?, approved_at=? WHERE id=?""",
        (actor, utc_now(), correction_id),
    )
    return dict(connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone())


def apply_transaction_correction(
    connection: sqlite3.Connection, correction_id: int, *, actor: str, check_cancelled=None
) -> dict[str, Any]:
    # The expected projection read and all resulting writes share one SQLite
    # snapshot. Concurrent source changes cannot slip between CAS and apply.
    connection.execute("SAVEPOINT transaction_grouping_snapshot")
    try:
        result = _apply_transaction_correction(connection, correction_id, actor=actor, check_cancelled=check_cancelled)
        connection.execute("RELEASE SAVEPOINT transaction_grouping_snapshot")
        return result
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT transaction_grouping_snapshot")
        connection.execute("RELEASE SAVEPOINT transaction_grouping_snapshot")
        raise


def _apply_transaction_correction(
    connection: sqlite3.Connection, correction_id: int, *, actor: str, check_cancelled=None
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "applied":
        return dict(row)
    if row["status"] != "approved":
        raise TransactionCorrectionError("Endast godkända rättningar får tillämpas")

    from .transaction_feed import _build_transaction_items

    if _decode(row['original_feed_json']).get('_instrument_category'):
        from .instrument_grouping import apply
        return apply(connection, row, actor=actor, check_cancelled=check_cancelled)
    expected = [int(value) for value in _decode(row["expected_document_ids_json"])]
    additional = [int(value) for value in _decode(row["additional_document_ids_json"])]
    affected = sorted(set(expected) | set(additional))
    current = next((i for i in _build_transaction_items(connection,issuer_ids=[row['issuer_id']],check_cancelled=check_cancelled) if i['id']==row['transaction_id']),None)
    if current is None:
        raise TransactionCorrectionError('Transaktionen saknas')
    if sorted(int(value) for value in current["source_document_ids"]) != sorted(expected):
        raise TransactionCorrectionError("Feedposten har ändrats sedan förslaget skapades")
    from .grouping_consolidation import candidate as consolidation_candidate, apply as apply_consolidation
    rounds = _decode(row['resulting_rounds_json'])
    if row['correction_type'] == 'split_transaction' and len(rounds) == 2:
        from .unit_compensation_split import prepare as prepare_unit_split, apply as apply_unit_split
        unit_cards = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
        unit_plan = prepare_unit_split(connection, row, rounds, unit_cards)
        if unit_plan is not None:
            return apply_unit_split(connection, row, rounds, unit_cards, unit_plan,
                                    actor=actor, check_cancelled=check_cancelled)
    if row['correction_type'] in {'split_transaction','rebuild_transaction'} and len(rounds)==2:
        consolidation_cards = _affected_feed_items(connection,affected,issuer_id=row['issuer_id'],check_cancelled=check_cancelled)
        if consolidation_candidate(connection,row,rounds,consolidation_cards):
            return apply_consolidation(connection,row,rounds,consolidation_cards,actor=actor,check_cancelled=check_cancelled)
    existing_assignments = effective_document_rounds(connection, affected)
    desired_assignments = {int(doc):group['canonical_round_key'] for group in _decode(row['resulting_rounds_json']) for doc in group['document_ids']}
    # Extend verified rounds without replacing prior overlays or their ownership.
    # Reassigning an already corrected source to a different round still fails.
    changed = {doc for doc, key in existing_assignments.items() if desired_assignments.get(doc) != key}
    replacing = []
    fresh = None
    if changed:
        if row['correction_type'] != 'rebuild_transaction':
            raise TransactionCorrectionError("Ett eller flera dokument har redan en aktiv grupprättning")
        # A rebuild may replace reviewed assignments only for complete old
        # rounds and the exact source/card inventory captured at proposal time.
        # Compare multisets: neither duplicate cards nor omitted sources vanish.
        def inventory(groups):
            return Counter((part['transaction_id'], part['canonical_round_key'], tuple(sorted(part['document_ids'])))
                           for part in groups)
        snapshot = _decode(row['original_feed_json'])
        expected_inventory = snapshot.get('_expected_grouping_inventory')
        if expected_inventory is None:
            # Legacy agent jobs already stored the exact expected grouping in
            # their immutable request. Recover it without rewriting that job.
            job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?',
                                     (correction_id,)).fetchone()
            if job:
                expected_inventory = json.loads(job[0])['payload'].get('expected_current_grouping')
        if expected_inventory is None:
            raise TransactionCorrectionError('grouping_rebuild_snapshot_missing')
        fresh = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
        if inventory(_grouping_inventory(fresh, set(affected))) != inventory(expected_inventory):
            raise TransactionCorrectionError('grouping_rebuild_expected_state_conflict')
        old_keys = {existing_assignments[doc] for doc in changed}
        active = connection.execute('SELECT * FROM financing_transaction_document_overrides WHERE issuer_id=? AND active=1',
                                    (row['issuer_id'],)).fetchall()
        covered = {int(previous['document_id']) for previous in active if previous['canonical_round_key'] in old_keys}
        if not covered.issubset(set(affected)):
            raise TransactionCorrectionError('grouping_rebuild_incomplete_previous_round')
        replacing = [previous for previous in active if int(previous['document_id']) in changed]
    if len(existing_assignments)==len(affected) and not changed:
        raise TransactionCorrectionError('Grupperingen är redan tillämpad')

    binding = None
    if row['correction_type'] == 'rebuild_transaction':
        if fresh is None:
            fresh = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
            expected_inventory = _decode(row['original_feed_json']).get('_expected_grouping_inventory')
            if expected_inventory is None:
                job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?', (correction_id,)).fetchone()
                if job:
                    expected_inventory = json.loads(job[0])['payload'].get('expected_current_grouping')
            def inventory(groups):
                return Counter((part['transaction_id'], part['canonical_round_key'], tuple(sorted(part['document_ids']))) for part in groups)
            if expected_inventory is None or inventory(_grouping_inventory(fresh, set(affected))) != inventory(expected_inventory):
                raise TransactionCorrectionError('grouping_rebuild_expected_state_conflict')
        binding = _prepare_rebuild_binding(connection, row, _decode(row['resulting_rounds_json']), fresh, affected)

    if fresh is None:
        fresh = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
    value_snapshot = _active_value_snapshot(connection, fresh)
    if row['correction_type'] == 'split_transaction':
        binding = _prepare_split_binding(connection, row, _decode(row['resulting_rounds_json']),
                                         fresh, affected, value_snapshot)

    from .radar_analysis import current_analysis, recalculate_adjusted_runway

    before_rows = current_analysis(connection, [int(row["issuer_id"])],check_cancelled=check_cancelled)
    before_snapshot = before_rows[0] if before_rows else None
    now = utc_now()
    connection.execute("SAVEPOINT transaction_grouping_apply")
    try:
        for previous in replacing:
            connection.execute('INSERT INTO financing_transaction_grouping_replacements VALUES(?,?)',
                               (correction_id, previous['id']))
            connection.execute('UPDATE financing_transaction_document_overrides SET active=0,reverted_at=? WHERE id=? AND active=1',
                               (now, previous['id']))
        for group in _decode(row["resulting_rounds_json"]):
            for document_id in group["document_ids"]:
                if int(document_id) in existing_assignments and int(document_id) not in changed:
                    continue
                connection.execute(
                    """INSERT INTO financing_transaction_document_overrides(
                           correction_id, issuer_id, document_id, canonical_round_key, created_at
                       ) VALUES(?,?,?,?,?)""",
                    (correction_id, row["issuer_id"], document_id, group["canonical_round_key"], now),
                )
        if binding is not None:
            connection.execute("""INSERT INTO financing_transaction_projection_bindings(
                correction_id,issuer_id,canonical_round_key,transaction_id,category,
                document_ids_json,neutral_document_ids_json,neutral_source_fingerprints_json,retired_transaction_ids_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (correction_id,row['issuer_id'],binding['canonical_round_key'],
                binding['transaction_id'],binding['category'],json.dumps(binding['document_ids']),
                json.dumps(binding['neutral_document_ids']),json.dumps(binding['neutral_source_fingerprints']),json.dumps(binding['retired_transaction_ids']),now))
        rebuilt = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'],check_cancelled=check_cancelled)
        if not grouping_projection_readback(_decode(row["resulting_rounds_json"]), rebuilt)["grouping_matches_applied"]:
            raise GroupingReadbackMismatch()
        _assert_value_provenance_preserved(value_snapshot, rebuilt)
        if before_snapshot is not None:
            recalculate_adjusted_runway(
                connection, int(row["issuer_id"]),
                idempotency_key=f"transaction-grouping:{correction_id}",
                actor=actor,
                reason=f"Grupprättning {correction_id}: {row['reason']}",
                correction_ids=[correction_id],
                before_snapshot=before_snapshot,
                check_cancelled=check_cancelled,
            )
        connection.execute(
            """UPDATE financing_transaction_corrections
               SET status='applied', applied_by=?, applied_at=?, applied_feed_json=?
               WHERE id=?""",
            (actor, now, json.dumps(rebuilt, ensure_ascii=False), correction_id),
        )
        connection.execute("RELEASE SAVEPOINT transaction_grouping_apply")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT transaction_grouping_apply")
        connection.execute("RELEASE SAVEPOINT transaction_grouping_apply")
        raise
    return dict(connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone())


def revert_transaction_correction(
    connection: sqlite3.Connection, correction_id: int, *, actor: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if row is None:
        raise KeyError(correction_id)
    if row["status"] == "reverted":
        return dict(row)
    if row["status"] != "applied":
        raise TransactionCorrectionError("Endast tillämpade rättningar kan återställas")
    if connection.execute('''SELECT 1 FROM financing_transaction_grouping_replacements r
            JOIN financing_transaction_document_overrides old ON old.id=r.previous_override_id
            JOIN financing_transaction_corrections newer ON newer.id=r.correction_id
            WHERE old.correction_id=? AND newer.status='applied' LIMIT 1''', (correction_id,)).fetchone():
        raise TransactionCorrectionError('Återställ senare omgruppering först')
    scoped = connection.execute('SELECT * FROM financing_transaction_instrument_bindings WHERE correction_id=? AND active=1', (correction_id,)).fetchone()
    if scoped is not None:
        added = set(_decode(row['additional_document_ids_json']))
        for patch in connection.execute('''SELECT v.source_document_id FROM financing_transaction_value_overrides o
            JOIN financing_transaction_value_corrections v ON v.id=o.correction_id
            WHERE o.active=1 AND o.transaction_id=?''', (row['transaction_id'],)):
            if patch['source_document_id'] in added:
                raise TransactionCorrectionError('instrument_grouping_dependent_value_override')
    consolidated = connection.execute('SELECT * FROM financing_transaction_grouping_consolidations WHERE correction_id=? AND active=1',(correction_id,)).fetchone()
    if consolidated is not None:
        plan=json.loads(consolidated['plan_json'])
        ids=sorted({p['transaction_id']for p in plan}|{o for p in plan for o in p['value_owners']})
        slots=','.join('?' for _ in ids)
        if connection.execute(f'SELECT 1 FROM financing_transaction_value_overrides WHERE transaction_id IN ({slots}) AND id>? LIMIT 1',[*ids,consolidated['max_value_row_id']]).fetchone():
            raise TransactionCorrectionError('grouping_consolidation_dependent_value_override')
    owned={int(r[0]) for r in connection.execute('SELECT document_id FROM financing_transaction_document_overrides WHERE correction_id=? AND active=1',(correction_id,))}
    for later in connection.execute("SELECT resulting_rounds_json FROM financing_transaction_corrections WHERE issuer_id=? AND id>? AND status='applied'",(row['issuer_id'],correction_id)):
        if any(owned.intersection(int(d) for d in group['document_ids']) for group in _decode(later[0])):
            raise TransactionCorrectionError('Återställ senare tilläggsrättningar först')
    from .radar_analysis import current_analysis, recalculate_adjusted_runway

    before_rows = current_analysis(connection, [int(row["issuer_id"])])
    before_snapshot = before_rows[0] if before_rows else None
    now = utc_now()
    connection.execute("SAVEPOINT transaction_grouping_revert")
    try:
        connection.execute(
            """UPDATE financing_transaction_document_overrides
               SET active=0, reverted_at=? WHERE correction_id=? AND active=1""",
            (now, correction_id),
        )
        connection.execute("UPDATE financing_transaction_instrument_bindings SET active=0,reverted_at=? WHERE correction_id=? AND active=1", (now,correction_id))
        connection.execute("UPDATE financing_transaction_grouping_consolidations SET active=0,reverted_at=? WHERE correction_id=? AND active=1", (now,correction_id))
        connection.execute("UPDATE financing_transaction_projection_bindings SET active=0,reverted_at=? WHERE correction_id=? AND active=1",
                           (now,correction_id))
        connection.execute('''UPDATE financing_transaction_document_overrides SET active=1,reverted_at=NULL
            WHERE id IN (SELECT previous_override_id FROM financing_transaction_grouping_replacements WHERE correction_id=?)''',
                           (correction_id,))
        if before_snapshot is not None:
            recalculate_adjusted_runway(
                connection, int(row["issuer_id"]),
                idempotency_key=f"transaction-grouping-revert:{correction_id}",
                actor=actor,
                reason=f"Återställ grupprättning {correction_id}",
                correction_ids=[],
                before_snapshot=before_snapshot,
            )
        connection.execute(
            """UPDATE financing_transaction_corrections
               SET status='reverted', reverted_by=?, reverted_at=? WHERE id=?""",
            (actor, now, correction_id),
        )
        connection.execute("RELEASE SAVEPOINT transaction_grouping_revert")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT transaction_grouping_revert")
        connection.execute("RELEASE SAVEPOINT transaction_grouping_revert")
        raise
    return dict(connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone())


def transaction_correction_detail(
    connection: sqlite3.Connection, correction_id: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM financing_transaction_corrections WHERE id=?", (correction_id,)
    ).fetchone()
    if row is None:
        raise KeyError(correction_id)
    item = dict(row)
    expected = [int(value) for value in _decode(item.pop("expected_document_ids_json"))]
    additional = [int(value) for value in _decode(
        item.pop("additional_document_ids_json", "[]")
    )]
    affected = sorted(set(expected) | set(additional))
    rounds = _decode(item.pop("resulting_rounds_json"))
    original_feed = _decode(item.pop("original_feed_json"))
    applied_feed = _decode(item.pop("applied_feed_json"))
    effective = effective_document_rounds(connection, affected)
    slots = ",".join("?" for _ in affected)
    applied_ids = sorted({int(value["correction_id"]) for value in connection.execute(
        f"""SELECT correction_id FROM financing_transaction_document_overrides
            WHERE active=1 AND document_id IN ({slots})""",
        affected,
    ).fetchall()})
    current_feed = _affected_feed_items(connection, affected)
    verification = reviewed_grouping_readback(connection, correction_id, rounds, current_feed)
    if item["status"] != "applied":
        verification.update(grouping_matches_applied=False, current_round_matches_applied=False,
                            effective_matches_applied=False)
    return {
        **item,
        **verification,
        "additional_document_ids": additional,
        "original_document_grouping": [{
            "transaction_id": item["transaction_id"], "document_ids": expected,
        }],
        "proposed_document_grouping": rounds,
        "effective_document_grouping": (verification["effective_document_grouping"]
            if item["status"] == "applied" else [
            {"canonical_round_key": group["canonical_round_key"],
             "document_ids": [doc for doc in group["document_ids"]
                              if effective.get(int(doc)) == group["canonical_round_key"]]}
            for group in rounds
        ] if effective else [{
            "transaction_id": item["transaction_id"], "document_ids": expected,
        }]),
        "canonical_round_by_document": {
            str(document_id): effective.get(document_id) for document_id in affected
        },
        "applied_correction_ids": applied_ids,
        "feed_before_correction": original_feed,
        "feed_after_correction": (
            current_feed
            if item["status"] == "applied" else applied_feed
        ),
        "effective_feed_current": current_feed,
    }


def rebuild_transaction(
    connection: sqlite3.Connection, transaction_id: str
) -> dict[str, Any]:
    """Rebuild and inspect affected feed rows; this performs no write."""
    from .transaction_feed import get_transaction_detail

    correction = connection.execute(
        """SELECT expected_document_ids_json
           FROM financing_transaction_corrections
           WHERE transaction_id=? AND status='applied'
           ORDER BY applied_at DESC, id DESC LIMIT 1""",
        (transaction_id,),
    ).fetchone()
    if correction is not None:
        document_ids = [int(value) for value in _decode(
            correction["expected_document_ids_json"]
        )]
    else:
        item = get_transaction_detail(connection, transaction_id)
        document_ids = [int(value) for value in item["source_document_ids"]]
    return {
        "requested_transaction_id": transaction_id,
        "source_document_ids": document_ids,
        "items": _affected_feed_items(connection, document_ids),
    }
