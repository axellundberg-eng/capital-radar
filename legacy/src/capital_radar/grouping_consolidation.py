"""Source-owned scalar consolidation within an explicit two-round split.

An apply-owned projection plan retains old scalar journals and original rows.
It never guesses between conflicting owners or creates financial events.
"""
from collections import Counter
import copy
import json
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS financing_transaction_grouping_consolidations (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), plan_json TEXT NOT NULL,
 fingerprints_json TEXT NOT NULL, max_value_row_id INTEGER NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);
'''


def _rows(c):
    try:
        return c.execute('''SELECT b.* FROM financing_transaction_grouping_consolidations b
            JOIN financing_transaction_corrections g ON g.id=b.correction_id
            WHERE b.active=1 AND g.status IN ('approved','applied') ORDER BY b.correction_id''').fetchall()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):return []
        raise


def _fingerprint(c, doc):
    from .transaction_corrections import _neutral_source_fingerprint, _neutral_archive_hash
    return [_neutral_source_fingerprint(c,doc),_neutral_archive_hash(c,doc)]


def source_current(c, row):
    saved=json.loads(row['fingerprints_json'])
    return bool(saved) and all(value[1] and value==_fingerprint(c,int(doc)) for doc,value in saved.items())


def bindings(c, assignments):
    from .transaction_corrections import TransactionCorrectionError
    result=[];claimed=set()
    for row in _rows(c):
        plan=json.loads(row['plan_json']);docs={d for p in plan for d in p['document_ids']}
        if not docs.intersection(assignments):continue
        if not all(assignments.get(d)==p['canonical_round_key'] for p in plan for d in p['document_ids']):
            raise TransactionCorrectionError('grouping_consolidation_membership_changed')
        if not source_current(c,row):
            raise TransactionCorrectionError('grouping_consolidation_source_changed')
        if docs & claimed:raise TransactionCorrectionError('grouping_consolidation_ambiguous')
        claimed.update(docs)
        for p in plan:
            result.append({**p,'correction_id':row['correction_id'],'neutral_document_ids':[],
                'changed_neutral_document_ids':[],'legacy_neutral_financing_event_ids':[],
                'retired_transaction_ids':[],'consolidated_value_owners':True})
    return result


def merged_overrides(c, ids, raw):
    """Only explicit, audited alias owners; later target corrections supersede them.

    No dictionary-per-round projection or ambiguous owner selection is used.
    A duplicate inherited field is refused even if its values happen to match.
    """
    from .transaction_value_corrections import _raw_transaction_overrides
    from .transaction_corrections import TransactionCorrectionError
    result=copy.deepcopy(raw)
    for row in _rows(c):
        for p in json.loads(row['plan_json']):
            target=p['transaction_id']
            aliases=[v for v in p['value_owners'] if v!=target]
            if target not in ids or not aliases:continue
            patches=_raw_transaction_overrides(c,aliases)
            combined={'values':{},'correction_ids':[],'field_correction_ids':{},'field_source_ids':{}}
            for owner in aliases:
                patch=patches.get(owner,{})
                if patch!=p['value_owners'][owner]:
                    raise TransactionCorrectionError('grouping_consolidation_owner_changed')
                if set(combined['values']) & set(patch.get('values',{})):
                    raise TransactionCorrectionError('grouping_consolidation_field_conflict')
                for k in ['values','field_correction_ids','field_source_ids']:combined[k].update(patch.get(k,{}))
            current=result.get(target,{})
            for k in ['values','field_correction_ids','field_source_ids']:combined[k].update(current.get(k,{}))
            combined['correction_ids']=sorted(set(combined['field_correction_ids'].values()))
            result[target]=combined
    return result


def candidate(c,row,rounds,cards):
    if row['correction_type'] not in {'split_transaction','rebuild_transaction'} or len(rounds)!=2 or len(cards)<3:return False
    from .transaction_corrections import _active_value_snapshot
    patches=_active_value_snapshot(c,cards)
    # Preserve existing single-owner split behavior; this path only consolidates
    # at least two previously independent scalar journals into the main round.
    for group in rounds:
        owners=[v for v,p in patches.items() if p.get('field_source_ids') and
                set(p['field_source_ids'].values()).issubset(group['document_ids'])]
        if len(owners)>=2:return True
    return False


def apply(c,row,rounds,cards,*,actor,check_cancelled=None):
    from .transaction_corrections import (TransactionCorrectionError,GroupingReadbackMismatch,
        _grouping_inventory,_active_value_snapshot,_affected_feed_items,effective_document_rounds,
        grouping_projection_readback)
    from .transaction_value_corrections import _value
    from .normalization import utc_now
    from .radar_analysis import current_analysis,recalculate_adjusted_runway
    def fail(code):raise TransactionCorrectionError('grouping_consolidation_'+code)
    docs=sorted(json.loads(row['expected_document_ids_json'])+json.loads(row['additional_document_ids_json']))
    snapshot=json.loads(row['original_feed_json']);expected=snapshot.get('_expected_grouping_inventory')
    if expected is None:
        job=c.execute('SELECT request_json FROM agent_proposal_jobs WHERE grouping_correction_id=?',(row['id'],)).fetchone()
        if job:expected=json.loads(job[0])['payload'].get('expected_current_grouping')
    counter=lambda parts:Counter((p['transaction_id'],p['canonical_round_key'],tuple(sorted(p['document_ids']))) for p in parts)
    if expected is None or counter(expected)!=counter(_grouping_inventory(cards,set(docs))):fail('expected_state_conflict')
    if Counter(d for card in cards for d in card['source_document_ids'])!=Counter(docs):fail('membership_changed')
    target=next((card for card in cards if card['id']==row['transaction_id']),None)
    if target is None or target['category']!='rights_issue':fail('separate_instrument')
    patches=_active_value_snapshot(c,cards);plans=[];used=set()
    for group in rounds:
        members=set(group['document_ids']);owners={}
        for owner,patch in patches.items():
            sources=set(patch.get('field_source_ids',{}).values())
            if sources & members:
                if not sources.issubset(members):fail('value_source_conflict')
                if patch['values'].get('effective_stages') or patch['values'].get('additional_instruments'):fail('structured_values')
                if 'canonical_round_key' in patch['values'] and patch['values']['canonical_round_key']!=group['canonical_round_key']:fail('identity_conflict')
                owners[owner]={k:copy.deepcopy(patch[k])for k in ['values','correction_ids','field_correction_ids','field_source_ids']}
        main=set(target['source_document_ids']) & members
        if main and any(patch['category']=='rights_issue' for owner,patch in patches.items() if owner in owners):
            stable=target
        else:
            choices=[card for card in cards if card['id'] in owners and set(card['source_document_ids']).issubset(members)]
            if len(choices)!=1:fail('ambiguous')
            stable=choices[0]
        # A single legal category per resulting instrument. Existing category
        # corrections remain on their stable identity, never copied to another.
        if any(patches[owner]['category']!=stable['category'] for owner in owners):fail('separate_instrument')
        if stable['category'] not in {'rights_issue','directed_issue'}:fail('separate_instrument')
        fields=[field for patch in owners.values()for field in patch['values']]
        if len(fields)!=len(set(fields)):fail('field_conflict')
        for card in cards:
            if set(card['source_document_ids']) & members and card['id'] not in owners and card['id']!=target['id']:
                # Uncorrected legacy rights cards can be partitioned; other
                # classified legal instruments cannot disappear into the plan.
                if card['category']!='rights_issue':fail('separate_instrument')
        if set(owners)&used:fail('ambiguous')
        used.update(owners)
        plans.append({**group,'transaction_id':stable['id'],
            'category':stable['original_transaction_values']['category'],'effective_category':stable['category'],
            'value_owners':owners,'consolidated_value_owners':True})
    if used!=set(patches) or len({p['transaction_id']for p in plans})!=len(plans):fail('value_source_conflict')
    # Refuse an existing scoped binding; its identity/proof must not be hidden.
    from .transaction_corrections import effective_projection_bindings
    old=effective_document_rounds(c,docs)
    if effective_projection_bindings(c,old):fail('existing_binding')
    assignments={d:p['canonical_round_key']for p in rounds for d in p['document_ids']}
    previous=c.execute('SELECT * FROM financing_transaction_document_overrides WHERE issuer_id=? AND active=1',(row['issuer_id'],)).fetchall()
    changed={r['canonical_round_key'] for r in previous if r['document_id'] in docs and assignments[r['document_id']]!=r['canonical_round_key']}
    if any(r['canonical_round_key'] in changed and r['document_id'] not in docs for r in previous):fail('incomplete_previous_round')
    fingerprints={str(d):_fingerprint(c,d)for d in docs}
    if any(v[1] is None for v in fingerprints.values()):fail('source_changed')
    before=current_analysis(c,[row['issuer_id']],check_cancelled=check_cancelled)
    maxrow=c.execute('SELECT COALESCE(MAX(id),0) FROM financing_transaction_value_overrides').fetchone()[0]
    now=utc_now();c.execute('SAVEPOINT grouping_consolidation_apply')
    try:
        for prior in previous:
            d=prior['document_id']
            if d in docs and assignments[d]!=prior['canonical_round_key']:
                c.execute('INSERT INTO financing_transaction_grouping_replacements VALUES(?,?)',(row['id'],prior['id']))
                c.execute('UPDATE financing_transaction_document_overrides SET active=0,reverted_at=? WHERE id=?',(now,prior['id']))
        for d in docs:
            if old.get(d)==assignments[d]:continue
            c.execute('INSERT INTO financing_transaction_document_overrides(correction_id,issuer_id,document_id,canonical_round_key,created_at) VALUES(?,?,?,?,?)',(row['id'],row['issuer_id'],d,assignments[d],now))
        c.execute('INSERT INTO financing_transaction_grouping_consolidations VALUES(?,?,?,?,?,1,?,NULL)',(row['id'],row['issuer_id'],json.dumps(plans),json.dumps(fingerprints),maxrow,now))
        rebuilt=_affected_feed_items(c,docs,issuer_id=row['issuer_id'],check_cancelled=check_cancelled)
        if not grouping_projection_readback(rounds,rebuilt)['grouping_matches_applied']:raise GroupingReadbackMismatch()
        for p in plans:
            actual=[card for card in rebuilt if card['id']==p['transaction_id']]
            if len(actual)!=1 or actual[0]['category']!=p['effective_category']:fail('identity_conflict')
            for owner,patch in p['value_owners'].items():
                for field,value in patch['values'].items():
                    holders=[card for card in rebuilt if patch['field_correction_ids'][field] in card['applied_transaction_correction_ids']]
                    if len(holders)!=1 or holders[0]['id']!=p['transaction_id'] or _value(holders[0],field)!=value or patch['field_source_ids'][field]not in holders[0]['source_document_ids']:fail('value_provenance_mismatch')
        if before:recalculate_adjusted_runway(c,row['issuer_id'],idempotency_key=f'transaction-grouping:{row["id"]}',actor=actor,reason=f'Grupprättning {row["id"]}: {row["reason"]}',correction_ids=[row['id']],before_snapshot=before[0],check_cancelled=check_cancelled)
        c.execute("UPDATE financing_transaction_corrections SET status='applied',applied_by=?,applied_at=?,applied_feed_json=? WHERE id=?",(actor,now,json.dumps(rebuilt,ensure_ascii=False),row['id']))
        c.execute('RELEASE SAVEPOINT grouping_consolidation_apply')
    except Exception:
        c.execute('ROLLBACK TO SAVEPOINT grouping_consolidation_apply');c.execute('RELEASE SAVEPOINT grouping_consolidation_apply');raise
    return dict(c.execute('SELECT * FROM financing_transaction_corrections WHERE id=?',(row['id'],)).fetchone())
