"""Audited unit/rights source partition without migrating value journals.

An uncorrected rights card can contain a compensation issue and historical
sources for a separately classified unit outcome. Round keys alone cannot
coalesce those categories. Pin the two existing identities to the explicit
source partition, using the existing source-fenced consolidation journal.
"""
from collections import Counter
import json
import re


def prepare(connection, row, rounds, cards):
    from .transaction_corrections import (
        TransactionCorrectionError, _active_value_snapshot, _grouping_inventory,
        _neutral_archive_hash, _neutral_source_fingerprint,
        _neutral_job_archive_current, effective_document_rounds,
    )

    def fail(code):
        raise TransactionCorrectionError('grouping_unit_split_' + code)

    if row['correction_type'] != 'split_transaction' or len(rounds) != 2 or len(cards) != 2:
        return None
    targets = [c for c in cards if c['id'] == row['transaction_id']]
    if len(targets) != 1 or targets[0]['category'] != 'unit_issue':
        return None
    target = targets[0]
    others = [c for c in cards if c['id'] != target['id']]
    if len(others) != 1 or others[0]['category'] != 'rights_issue':
        return None
    # Existing audited values need the separate ownership-preserving paths.
    if _active_value_snapshot(connection, cards):
        return None
    affected = sorted(json.loads(row['expected_document_ids_json']) +
                      json.loads(row['additional_document_ids_json']))
    expected = json.loads(row['original_feed_json']).get('_expected_grouping_inventory')
    if expected is None:
        job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?',
                                 (row['id'],)).fetchone()
        if job:
            expected = json.loads(job[0])['payload'].get('expected_current_grouping')
    inventory = lambda parts: Counter((p['transaction_id'], p['canonical_round_key'],
                                     tuple(sorted(p['document_ids']))) for p in parts)
    if expected is None or inventory(expected) != inventory(_grouping_inventory(cards, set(affected))):
        fail('expected_state_conflict')
    if Counter(d for c in cards for d in c['source_document_ids']) != Counter(affected):
        fail('membership_changed')
    native = set(target['source_document_ids'])
    main = [r for r in rounds if native.issubset(r['document_ids'])]
    if len(main) != 1:
        fail('membership_changed')
    main = main[0]
    fee = [r for r in rounds if r is not main][0]
    if (native == set(main['document_ids']) or not fee['document_ids']
            or not set(fee['document_ids']).issubset(others[0]['source_document_ids'])):
        fail('membership_changed')
    if effective_document_rounds(connection, affected):
        fail('existing_assignment')
    slots = ','.join('?' for _ in affected)
    if connection.execute('SELECT 1 FROM financing_transaction_document_links '
                          'WHERE active=1 AND document_id IN ('+slots+') LIMIT 1', affected).fetchone():
        fail('separate_instrument')

    fingerprints = {}
    for doc in affected:
        archive_hash = _neutral_archive_hash(connection, doc)
        if not archive_hash or _neutral_job_archive_current(connection, row['id'], doc) is not True:
            fail('source_changed')
        text = connection.execute('SELECT full_text FROM document_evidence_texts '
                                 'WHERE document_id=? ORDER BY id DESC LIMIT 1', (doc,)).fetchone()
        title = connection.execute('SELECT title FROM documents WHERE id=?', (doc,)).fetchone()
        if text is None or title is None:
            fail('source_changed')
        # The stored digest may identify the extraction artifact. Check the
        # immutable evidence rows and quoted offsets, then pin a computed hash
        # of the full archive. Never rewrite the artifact's original digest.
        job = connection.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?',
                                 (row['id'],)).fetchone()
        proofs = [p for p in json.loads(job[0])['payload']['evidence_segments'] if p['document_id']==doc]
        if not proofs or any(text[0][p['start']:p['end']] != p['source_excerpt'] for p in proofs):
            fail('source_changed')
        body = re.sub(r'\s+', ' ', ' '.join(p['source_excerpt'] for p in proofs)).casefold()
        unit = re.search(r'\bunits?\b|unitemission', body)
        compensation = re.search(r'compensation issue|ersättningsemission', body)
        if doc in fee['document_ids']:
            if not (unit and compensation and re.search(r'\bguarant\w*|\bgarant\w*', body)
                    and re.search(r'set[ -]?off|offsetting|kvitt\w*', body)):
                fail('source_scope')
        elif not (unit and re.search(r'rights issue|företrädesemission', body)) or re.search(
                r'compensation issue|ersättningsemission', title[0], re.I):
            fail('source_scope')
        # No hidden loan, warrant or other separately extracted security is
        # swallowed by a source-category binding. These remain unsupported.
        allowed = {'rights_issue', 'unit_issue'}
        if doc in fee['document_ids']:
            allowed.add('directed_issue')
        for event in connection.execute('''SELECT DISTINCT f.financing_type FROM financing_events f
            JOIN financing_event_evidence e ON e.financing_event_id=f.id WHERE e.document_id=?''', (doc,)):
            if event[0] and event[0] not in allowed:
                fail('separate_instrument')
        fingerprints[str(doc)] = [_neutral_source_fingerprint(connection, doc), archive_hash]
    plans = [
        {**main, 'transaction_id': target['id'], 'category': target['category'],
         'effective_category': target['category'], 'value_owners': {}},
        {**fee, 'transaction_id': others[0]['id'], 'category': others[0]['category'],
         'effective_category': others[0]['category'], 'value_owners': {}},
    ]
    return plans, fingerprints


def apply(connection, row, rounds, cards, prepared, *, actor, check_cancelled=None):
    from .transaction_corrections import (
        TransactionCorrectionError, GroupingReadbackMismatch,
        _affected_feed_items, grouping_projection_readback,
    )
    from .radar_analysis import current_analysis, recalculate_adjusted_runway
    from .normalization import utc_now

    plans, fingerprints = prepared
    before = current_analysis(connection, [row['issuer_id']], check_cancelled=check_cancelled)
    max_value = connection.execute('SELECT COALESCE(MAX(id),0) FROM financing_transaction_value_overrides').fetchone()[0]
    now = utc_now()
    connection.execute('SAVEPOINT grouping_unit_split_apply')
    try:
        for group in rounds:
            for doc in group['document_ids']:
                connection.execute('''INSERT INTO financing_transaction_document_overrides
                    (correction_id,issuer_id,document_id,canonical_round_key,created_at) VALUES(?,?,?,?,?)''',
                    (row['id'], row['issuer_id'], doc, group['canonical_round_key'], now))
        connection.execute('INSERT INTO financing_transaction_grouping_consolidations VALUES(?,?,?,?,?,1,?,NULL)',
                           (row['id'], row['issuer_id'], json.dumps(plans), json.dumps(fingerprints), max_value, now))
        affected = [doc for group in rounds for doc in group['document_ids']]
        rebuilt = _affected_feed_items(connection, affected, issuer_id=row['issuer_id'], check_cancelled=check_cancelled)
        if not grouping_projection_readback(rounds, rebuilt)['grouping_matches_applied']:
            raise GroupingReadbackMismatch()
        actual = Counter((c['id'], c['category'], tuple(sorted(c['source_document_ids']))) for c in rebuilt)
        expected = Counter((p['transaction_id'], p['effective_category'], tuple(sorted(p['document_ids']))) for p in plans)
        if actual != expected:
            raise TransactionCorrectionError('grouping_unit_split_identity_conflict')
        if before:
            recalculate_adjusted_runway(connection, row['issuer_id'],
                idempotency_key=f'transaction-grouping:{row["id"]}', actor=actor,
                reason=f'Grupprättning {row["id"]}: {row["reason"]}', correction_ids=[row['id']],
                before_snapshot=before[0], check_cancelled=check_cancelled)
        connection.execute("UPDATE financing_transaction_corrections SET status='applied',applied_by=?,applied_at=?,applied_feed_json=? WHERE id=?",
                           (actor, now, json.dumps(rebuilt, ensure_ascii=False), row['id']))
        connection.execute('RELEASE SAVEPOINT grouping_unit_split_apply')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT grouping_unit_split_apply')
        connection.execute('RELEASE SAVEPOINT grouping_unit_split_apply')
        raise
    return dict(connection.execute('SELECT * FROM financing_transaction_corrections WHERE id=?', (row['id'],)).fetchone())


def diagnostics(connection, row, rounds, cards):
    """Current prerequisites only; never infer an old failure's internal cause."""
    from .transaction_corrections import safe_grouping_error_code
    result = dict(observation_scope='current_prerequisites_not_historical_failure_cause',
                  read_only=True, current_unit_split_guard_passed=None,
                  current_validation_code='not_applicable', complete_apply_readiness_verified=False)
    try:
        plan = prepare(connection, row, rounds, cards)
    except ValueError as error:
        code = safe_grouping_error_code(error)
        result.update(current_unit_split_guard_passed=False if code else None,
                      current_validation_code=code or 'unit_split_guard_unclassified')
    else:
        if plan is not None:
            result.update(current_unit_split_guard_passed=True,
                          current_validation_code='unit_split_guard_passed')
    return result
