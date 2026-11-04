"""Audited presentation groups. Legal transactions/cash effects stay separate."""
import copy
import json
import re

from .normalization import utc_now
from .evidence_store import source_text_available


class NotificationGroupError(ValueError):
    def __init__(self, code: str, *, status: int = 422) -> None:
        super().__init__(code)
        self.code, self.status = code, status


SCHEMA = """
CREATE TABLE IF NOT EXISTS financing_notification_corrections(
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, issuer_id INTEGER NOT NULL,
 group_key TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL, rationale TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS notification_audit_no_update BEFORE UPDATE ON financing_notification_corrections
 BEGIN SELECT RAISE(ABORT,'immutable_notification_audit'); END;
CREATE TRIGGER IF NOT EXISTS notification_audit_no_delete BEFORE DELETE ON financing_notification_corrections
 BEGIN SELECT RAISE(ABORT,'immutable_notification_audit'); END;
CREATE TABLE IF NOT EXISTS financing_notification_members(
 transaction_id TEXT PRIMARY KEY, issuer_id INTEGER NOT NULL, group_key TEXT NOT NULL,
 correction_id INTEGER NOT NULL REFERENCES financing_notification_corrections(id),
 ordinal INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS notification_members_issuer ON financing_notification_members(issuer_id,group_key);
"""

def state(c, issuer_id):
    return {r['transaction_id']:r['group_key'] for r in c.execute(
        'SELECT transaction_id,group_key FROM financing_notification_members WHERE issuer_id=? ORDER BY transaction_id',(issuer_id,))}

def validate(c, p, cards):
    fields = {'run_id','lease_token','issuer_id','group_key','transaction_ids','expected_groups',
              'evidence_segments','confidence','rationale','revision_id'}
    if set(p)!=fields or type(p['issuer_id']) is not int or p['issuer_id']<1:
        raise NotificationGroupError('invalid_notification_fields')
    if not isinstance(p['group_key'],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}',p['group_key']):
        raise NotificationGroupError('invalid_notification_key')
    ids = p['transaction_ids']
    if not isinstance(ids,list) or not 2<=len(ids)<=50 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
        raise NotificationGroupError('invalid_notification_members')
    current = state(c,p['issuer_id'])
    if p['expected_groups'] != {i:current.get(i) for i in ids}:
        raise NotificationGroupError('notification_expected_state_conflict',status=409)
    if any(current.get(i) not in (None,p['group_key']) for i in ids):
        raise NotificationGroupError('notification_member_already_grouped',status=409)
    existing = {i for i,k in current.items() if k==p['group_key']}
    if existing-set(ids):
        raise NotificationGroupError('notification_existing_members_omitted',status=409)
    if type(p['confidence']) not in (float,int) or not .95<=p['confidence']<=1:
        raise NotificationGroupError('confidence_below_threshold')
    if not isinstance(p['rationale'],str) or not 8<=len(p['rationale'])<=1200:
        raise NotificationGroupError('notification_rationale_required')
    by_id = {item['id']:item for item in cards}
    if any(i not in by_id for i in ids):
        raise NotificationGroupError('notification_transaction_missing',status=409)
    segments = p['evidence_segments']
    if not isinstance(segments,list) or not 1<=len(segments)<=100:
        raise NotificationGroupError('notification_evidence_required')
    covered = set()
    for segment in segments:
        if not isinstance(segment,dict) or set(segment)!={'document_id','evidence_id','start','end','source_excerpt'}:
            raise NotificationGroupError('invalid_evidence_segment')
        if any(type(segment[k]) is not int for k in ('document_id','evidence_id','start','end')):
            raise NotificationGroupError('invalid_evidence_segment')
        archived = c.execute('SELECT e.*,d.issuer_id FROM document_evidence_texts e JOIN documents d ON d.id=e.document_id WHERE e.document_id=? ORDER BY e.id DESC LIMIT 1', (segment['document_id'],)).fetchone()
        a,b=segment['start'],segment['end']
        if not archived or archived['issuer_id']!=p['issuer_id'] or archived['id']!=segment['evidence_id'] or a<0 or b<=a or b>len(archived['full_text']) or archived['full_text'][a:b]!=segment['source_excerpt'] or len(segment['source_excerpt'])<12:
            raise NotificationGroupError('notification_evidence_mismatch')
        if not source_text_available(archived['full_text']) or not source_text_available(segment['source_excerpt']):
            raise NotificationGroupError('source_text_unavailable')
        covered.add(segment['document_id'])
    if any(not covered.intersection(by_id[i].get('source_document_ids',[])) for i in ids):
        raise NotificationGroupError('notification_member_evidence_missing')

def apply(c,p,actor):
    before = {i:state(c,p['issuer_id']).get(i) for i in p['transaction_ids']}
    after = {i:p['group_key'] for i in p['transaction_ids']}
    aid = c.execute('INSERT INTO financing_notification_corrections(actor,issuer_id,group_key,before_json,after_json,evidence_json,rationale,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (actor,p['issuer_id'],p['group_key'],json.dumps(before),json.dumps(after),json.dumps(p['evidence_segments']),p['rationale'],utc_now())).lastrowid
    for ordinal,tid in enumerate(p['transaction_ids']):
        c.execute('INSERT INTO financing_notification_members VALUES(?,?,?,?,?) ON CONFLICT(transaction_id) DO UPDATE SET correction_id=excluded.correction_id,ordinal=excluded.ordinal',
                  (tid,p['issuer_id'],p['group_key'],aid,ordinal))
    return {'status':'applied','audit_id':aid,'group_key':p['group_key'],'transaction_ids':p['transaction_ids'],
            'financial_transactions_unchanged':True,'cash_effects_unchanged':True,'reported_runway_unchanged':True,
            'readback_required':True}

def notifications(c, items):
    """Executed in background before sorting/pagination; never runway inputs."""
    by_id = {i['id']:i for i in items}
    groups = {}
    for row in c.execute('SELECT * FROM financing_notification_members ORDER BY group_key,ordinal'):
        if row['transaction_id'] in by_id:
            groups.setdefault(row['group_key'],[]).append(row['transaction_id'])
    hidden = set(); result = []
    for key,ids in groups.items():
        if len(ids)<2:
            continue
        primary = copy.deepcopy(by_id[ids[0]])
        children = [copy.deepcopy(by_id[i]) for i in ids[1:]]
        primary['original_published_at'] = primary['published_at']
        primary['published_at'] = max(str(by_id[i].get('published_at') or '') for i in ids)
        primary['notification_group_key'] = key
        primary['related_transactions'] = children
        primary['notification_transaction_ids'] = ids
        primary['notification_count'] = len(ids)
        result.append(primary); hidden.update(ids)
    result.extend(copy.deepcopy(i) for i in items if i['id'] not in hidden)
    return sorted(result,key=lambda i:(str(i.get('published_at') or ''),i['id']),reverse=True)
