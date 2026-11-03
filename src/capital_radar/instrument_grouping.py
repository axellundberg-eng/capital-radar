"""Audited, category-scoped membership for a shared financing source.

The initial contract is deliberately limited to one unexercised warrant round.
It does not replace the document-wide grouping contract or merge value owners.
"""
from __future__ import annotations

from collections import Counter
import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_transaction_instrument_bindings (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 transaction_id TEXT NOT NULL, category TEXT NOT NULL,
 canonical_round_key TEXT NOT NULL, document_ids_json TEXT NOT NULL,
 source_fingerprints_json TEXT NOT NULL,
 preserved_membership_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);
"""


def _membership(cards):
    return Counter((x['id'], x['category'],
                    (x.get('effective_transaction_values') or {}).get('canonical_round_key'),
                    tuple(sorted(x['source_document_ids']))) for x in cards)


def _fingerprint(c, doc):
    from .transaction_corrections import _neutral_source_fingerprint, _neutral_archive_hash
    archive = _neutral_archive_hash(c, doc)
    if archive is None:
        raise ValueError('instrument_grouping_source_changed')
    return [_neutral_source_fingerprint(c, doc), archive]


def bindings(c, issuer_ids=None):
    """Do not use a stale source premise, or select an overlapping last row."""
    try:
        rows = c.execute("""SELECT b.* FROM financing_transaction_instrument_bindings b
            JOIN financing_transaction_corrections g ON g.id=b.correction_id
            WHERE b.active=1 AND g.status IN ('approved','applied') ORDER BY b.correction_id""").fetchall()
    except Exception as error:
        import sqlite3
        if isinstance(error, sqlite3.OperationalError) and 'no such table' in str(error):
            return []
        raise
    result, owned = [], set()
    for row in rows:
        if issuer_ids is not None and row['issuer_id'] not in issuer_ids:
            continue
        part = dict(row)
        docs = json.loads(part['document_ids_json'])
        slots = {(doc, part['category']) for doc in docs}
        if slots & owned:
            raise ValueError('instrument_grouping_ambiguous')
        owned.update(slots)
        fingerprints = json.loads(part['source_fingerprints_json'])
        part['source_current'] = all(fingerprints.get(str(doc)) == _fingerprint(c, doc) for doc in docs)
        part['document_ids'] = docs
        result.append(part)
    return result


def _protected(card):
    return {k: card.get(k) for k in ('id', 'category', 'source_document_ids',
        'original_transaction_values', 'effective_transaction_values',
        'applied_transaction_correction_ids')}


def apply(c, row, *, actor, check_cancelled=None):
    from .transaction_corrections import (
        TransactionCorrectionError, GroupingReadbackMismatch, _affected_feed_items,
        _grouping_inventory, _active_value_snapshot, _assert_value_provenance_preserved)
    from .transaction_value_corrections import effective_transaction_overrides
    from .normalization import utc_now
    from .radar_analysis import current_analysis, recalculate_adjusted_runway

    snapshot = json.loads(row['original_feed_json'])
    category = snapshot.get('_instrument_category')
    rounds = json.loads(row['resulting_rounds_json'])
    expected = json.loads(row['expected_document_ids_json'])
    docs = sorted(expected + json.loads(row['additional_document_ids_json']))
    if category != 'warrant_exercise' or row['correction_type'] != 'rebuild_transaction' or len(rounds) != 1:
        raise TransactionCorrectionError('instrument_grouping_contract')
    cards = _affected_feed_items(c, docs, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
    inventory = lambda parts: Counter((x['transaction_id'], x['canonical_round_key'], tuple(x['document_ids'])) for x in parts)
    if inventory(_grouping_inventory(cards, set(docs))) != inventory(snapshot['_expected_grouping_inventory']):
        raise TransactionCorrectionError('instrument_grouping_expected_state_conflict')
    targets = [x for x in cards if x['id'] == row['transaction_id']]
    if len(targets) != 1 or targets[0]['category'] != category or sorted(targets[0]['source_document_ids']) != expected:
        raise TransactionCorrectionError('instrument_grouping_expected_state_conflict')
    selected = [x for x in cards if x['category'] == category]
    preserved = [x for x in cards if x['category'] != category]
    if (set(d for x in selected for d in x['source_document_ids']) != set(docs)
            or any(not set(x['source_document_ids']).issubset(docs) for x in selected)
            or any((x.get('effective_transaction_values') or {}).get('confirmed_cash_effect_amount') not in (None, 0) for x in selected)):
        raise TransactionCorrectionError('instrument_grouping_separate_instrument')
    from .transaction_feed import _SERIES
    series = {''.join(m.group(0).upper().split()) for x in selected
              for h in x.get('history', []) for m in _SERIES.finditer(h['title'])}
    if len(series) > 1:
        raise TransactionCorrectionError('instrument_grouping_separate_instrument')
    key = rounds[0]['canonical_round_key']
    if any((x.get('effective_transaction_values') or {}).get('canonical_round_key') == key for x in preserved):
        raise TransactionCorrectionError('instrument_grouping_separate_instrument')
    retired = [x['id'] for x in selected if x['id'] != row['transaction_id']]
    patches = effective_transaction_overrides(c, retired)
    if any(x.get('values') for x in patches.values()):
        raise TransactionCorrectionError('instrument_grouping_retired_value_override')
    from .transaction_corrections import effective_document_rounds
    if effective_document_rounds(c, docs) or any(set(x['document_ids']) & set(docs) for x in bindings(c, [row['issuer_id']])):
        raise TransactionCorrectionError('instrument_grouping_existing_assignment')
    values = _active_value_snapshot(c, cards)
    fingerprints = {str(doc): _fingerprint(c, doc) for doc in docs}
    old_membership = [dict(transaction_id=x['id'], category=x['category'],
        canonical_round_key=(x.get('effective_transaction_values') or {}).get('canonical_round_key'),
        document_ids=sorted(x['source_document_ids'])) for x in preserved]
    before = current_analysis(c, [row['issuer_id']], check_cancelled=check_cancelled)
    now = utc_now()
    c.execute('SAVEPOINT instrument_grouping_apply')
    try:
        c.execute("""INSERT INTO financing_transaction_instrument_bindings
            (correction_id,issuer_id,transaction_id,category,canonical_round_key,
             document_ids_json,source_fingerprints_json,preserved_membership_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (row['id'],row['issuer_id'],row['transaction_id'],category,key,
                json.dumps(docs),json.dumps(fingerprints),json.dumps(old_membership),now))
        rebuilt = _affected_feed_items(c, docs, issuer_id=row['issuer_id'],check_cancelled=check_cancelled)
        # Validate full cardinality, every scoped source, and unchanged other instruments
        # before any calculation. No dict keyed by round hides duplicate projections.
        actual_selected = [x for x in rebuilt if x['category'] == category]
        expected_membership = Counter([(row['transaction_id'],category,key,tuple(docs))])
        if _membership(actual_selected) != expected_membership:
            raise GroupingReadbackMismatch()
        others = [x for x in rebuilt if x['category'] != category]
        if (_membership(others) != _membership(preserved)
                or sorted(json.dumps(_protected(x),sort_keys=True) for x in others) !=
                   sorted(json.dumps(_protected(x),sort_keys=True) for x in preserved)):
            raise TransactionCorrectionError('instrument_grouping_other_instrument_changed')
        _assert_value_provenance_preserved(values, rebuilt)
        if before:
            recalculate_adjusted_runway(c, row['issuer_id'], idempotency_key=f"transaction-grouping:{row['id']}",
                actor=actor,reason=f"Grupprättning {row['id']}: {row['reason']}",correction_ids=[row['id']],
                before_snapshot=before[0],check_cancelled=check_cancelled)
        c.execute("""UPDATE financing_transaction_corrections SET status='applied',
            applied_by=?,applied_at=?,applied_feed_json=? WHERE id=?""",
            (actor,now,json.dumps(rebuilt,ensure_ascii=False),row['id']))
        c.execute('RELEASE SAVEPOINT instrument_grouping_apply')
    except Exception:
        c.execute('ROLLBACK TO SAVEPOINT instrument_grouping_apply')
        c.execute('RELEASE SAVEPOINT instrument_grouping_apply')
        raise
    return dict(c.execute('SELECT * FROM financing_transaction_corrections WHERE id=?',(row['id'],)).fetchone())


def readback(c, row, rounds, cards):
    from .transaction_corrections import grouping_projection_readback
    snapshot = json.loads(row['original_feed_json'])
    category = snapshot['_instrument_category']
    selected = [x for x in cards if x['category'] == category]
    other = [x for x in cards if x['category'] != category]
    part = grouping_projection_readback(rounds, selected)
    saved = c.execute('SELECT * FROM financing_transaction_instrument_bindings WHERE correction_id=? AND active=1', (row['id'],)).fetchone()
    expected_other = json.loads(saved['preserved_membership_json']) if saved else []
    expected = Counter((x['transaction_id'],x['category'],x['canonical_round_key'],tuple(x['document_ids'])) for x in expected_other)
    source = next((x for x in bindings(c,[row['issuer_id']]) if x['correction_id']==row['id']),None)
    ok = bool(row['status']=='applied' and saved and source and source['source_current']
              and _membership(other)==expected and part['grouping_matches_applied']
              and len(selected)==1 and selected[0]['id']==row['transaction_id'])
    actual = [dict(transaction_id=x['id'],category=x['category'],
        canonical_round_key=(x.get('effective_transaction_values') or {}).get('canonical_round_key'),
        document_ids=sorted(x['source_document_ids'])) for x in cards]
    return {**part,'verification_scope':'instrument_grouping',
        'effective_matches_applied':ok,'current_round_matches_applied':ok,'grouping_matches_applied':ok,
        'current_round_value':actual,'expected_current_document_grouping':rounds,
        'audited_extension_correction_ids':[],
        'preserved_instruments_match':_membership(other)==expected,
        'instrument_sources_unchanged':bool(source and source['source_current'])}
