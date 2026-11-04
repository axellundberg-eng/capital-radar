"""Exact archived evidence for every source in a reviewed lifecycle timeline."""
import hashlib
import json
import re
import sqlite3

SCHEMA='''
CREATE TABLE IF NOT EXISTS financing_transaction_stage_evidence (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_value_corrections(id),
 evidence_json TEXT NOT NULL
);
'''


def shape(proof):
    fields={'document_id','evidence_id','text_sha256','start','end','source_excerpt'}
    if not isinstance(proof,list) or not 1<=len(proof)<=30:raise ValueError('stage_evidence_required')
    spans={}
    for p in proof:
        if not isinstance(p,dict)or set(p)!=fields:raise ValueError('stage_evidence_contract')
        if any(type(p[k]) is not int for k in ['document_id','evidence_id','start','end']):raise ValueError('stage_evidence_span')
        if min(p['document_id'],p['evidence_id'])<1 or p['start']<0 or not p['start']<p['end']<=p['start']+5000:raise ValueError('stage_evidence_span')
        if not isinstance(p['text_sha256'],str)or not re.fullmatch('[0-9a-f]{64}',p['text_sha256']):raise ValueError('stage_evidence_hash')
        if not isinstance(p['source_excerpt'],str)or not 12<=len(p['source_excerpt'])<=5000 or len(p['source_excerpt'])!=p['end']-p['start']:raise ValueError('stage_evidence_span')
        old=spans.setdefault(p['document_id'],[])
        if any(p['start']<end and start<p['end']for start,end in old):raise ValueError('stage_evidence_overlap')
        old.append((p['start'],p['end']))
    return proof


def validate_timeline(proposed):
    """Pure event identity/order check shared by admission and application."""
    from datetime import datetime,timezone
    dates=[];events=set()
    for stage in proposed:
        stamp=datetime.fromisoformat(stage['effective_at'].replace('Z','+00:00'))
        stamp=stamp.replace(tzinfo=timezone.utc)if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        key=(stage['lifecycle_stage'],stamp)
        if key in events:raise ValueError('stage_evidence_duplicate_event')
        events.add(key);dates.append(stamp)
    if dates!=sorted(dates):raise ValueError('stage_evidence_unordered_timeline')


def timeline_diagnostics(proposed):
    """Current payload validity only; never a historical cause or apply receipt."""
    from .transaction_value_corrections import _validate_proposed
    code = None
    try:
        _validate_proposed('effective_stages', proposed)
        validate_timeline(proposed)
    except (ValueError, TypeError, KeyError) as error:
        code = str(error)
        if code not in {'stage_evidence_duplicate_event', 'stage_evidence_unordered_timeline'}:
            code = 'invalid_proposed_value'
    return {'observation_scope': 'current_timeline_only_not_historical_failure_cause',
            'read_only': True, 'timeline_valid': code is None,
            'current_validation_code': code, 'complete_apply_readiness_verified': False}


def verify(c,proof,proposed,item,primary,excerpt):
    from .evidence_store import source_text_available
    shape(proof)
    validate_timeline(proposed)
    sources={d for stage in proposed for d in stage['source_document_ids']}
    if {p['document_id']for p in proof}!=sources or primary not in sources:raise ValueError('stage_evidence_coverage')
    if not sources.issubset(item['source_document_ids']):raise ValueError('stage_evidence_source_scope')
    if not any(p['document_id']==primary and p['source_excerpt']==excerpt for p in proof):raise ValueError('stage_evidence_primary_excerpt')
    for p in proof:
        row=c.execute('''SELECT e.id,e.full_text,d.issuer_id FROM document_evidence_texts e
            JOIN documents d ON d.id=e.document_id WHERE e.document_id=? ORDER BY e.id DESC LIMIT 1''',(p['document_id'],)).fetchone()
        if row is None or row['id']!=p['evidence_id'] or row['issuer_id']!=item['issuer_id']:raise ValueError('stage_evidence_changed')
        text=row['full_text']
        if not source_text_available(text)or not source_text_available(p['source_excerpt']):raise ValueError('stage_evidence_unusable')
        if hashlib.sha256(text.encode('utf-8')).hexdigest()!=p['text_sha256']:raise ValueError('stage_evidence_changed')
        if text[p['start']:p['end']]!=p['source_excerpt']:raise ValueError('stage_evidence_span_mismatch')
    return True


def load(c,correction_id):
    try:r=c.execute('SELECT evidence_json FROM financing_transaction_stage_evidence WHERE correction_id=?',(correction_id,)).fetchone()
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):return None
        raise
    return json.loads(r[0])if r else None


def save(c,correction_id,proof):
    if proof is None:return
    encoded=json.dumps(shape(proof),ensure_ascii=False,sort_keys=True,separators=(',',':'))
    old=load(c,correction_id)
    if old is not None and old!=proof:raise ValueError('stage_evidence_receipt_conflict')
    c.execute('INSERT OR IGNORE INTO financing_transaction_stage_evidence VALUES(?,?)',(correction_id,encoded))


def current(c,correction_id,request,card):
    proof=load(c,correction_id)
    if card is None or proof is None or proof!=request.get('stage_evidence'):return False
    try:return verify(c,proof,request['proposed_value'],card,request['source_document_id'],request['source_excerpt'])
    except (ValueError,TypeError,KeyError):return False
