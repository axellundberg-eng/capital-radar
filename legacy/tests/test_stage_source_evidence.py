"""Per-stage archives must survive exact admission, apply, and readback checks."""
import copy
import hashlib
import json
import os
import unittest
from unittest.mock import patch
import test_transaction_feed as fixtures
from capital_radar.transaction_feed import _build_transaction_items
from capital_radar.transaction_value_corrections import create_transaction_value_correction,approve_transaction_value_correction,apply_transaction_value_correction
from capital_radar.evidence_store import ensure_document_evidence

class StageSourceEvidenceTests(unittest.TestCase):
    setUp=fixtures.TransactionFeedTests.setUp
    add=fixtures.TransactionFeedTests.add
    add_event=fixtures.TransactionFeedTests.add_event
    def case(self):
        self.docs=[];self.texts=['Åä🙂 Board resolves on a rights issue of SEK 57.5 million.','The same rights issue of SEK 57.5 million has been registered. '+' archive tail '*1800]
        for i,text in enumerate(self.texts):
            doc,_=self.add('Company resolves on rights issue'if i==0 else'Company rights issue registered',body=text,date=f'2026-09-{i+1:02}T08:00:00Z',language='en');self.docs.append(doc)
            self.add_event(doc,round_key='same-rights-round',stage='announced'if i==0 else'registered',gross_amount=57500000,liquidity_effect_kind='conditional_liquidity')
        with self.db.connect()as c:
            card=next(x for x in _build_transaction_items(c)if x['category']=='rights_issue');self.target=card['id']
            stages=[dict(lifecycle_stage=s,effective_at=f'2026-09-{i+1:02}',source_document_ids=[self.docs[i]],reported_values={'gross_amount':57500000})for i,s in enumerate(['announced','registered'])]
            proof=[]
            for i,doc in enumerate(self.docs):
                ensure_document_evidence(c,doc);e=c.execute('SELECT id,full_text FROM document_evidence_texts WHERE document_id=? ORDER BY id DESC LIMIT 1',(doc,)).fetchone();start=4 if i==0 else 0;end=len(self.texts[i])if i==0 else self.texts[i].index(' archive tail')
                proof.append(dict(document_id=doc,evidence_id=e['id'],text_sha256=hashlib.sha256(e['full_text'].encode()).hexdigest(),start=start,end=end,source_excerpt=e['full_text'][start:end]))
            self.payload=dict(transaction_id=self.target,issuer_id=card['issuer_id'],field_name='effective_stages',expected_previous_value=card['effective_transaction_values']['effective_stages'],proposed_value=stages,source_document_id=self.docs[0],source_excerpt=proof[0]['source_excerpt'],confidence=.99,rationale='Preserve distinct decision and registration sources without inventing receipt of cash.',stage_evidence=proof)
    def create(self,c,p=None):
        r=create_transaction_value_correction(c,p or self.payload,actor='test');approve_transaction_value_correction(c,r['id'],actor='test');return r['id']
    def test_two_sources_apply_preserves_raw_sources_and_original_stages(self):
        self.case()
        with self.db.connect()as c:
            raw={t:[tuple(r)for r in c.execute('SELECT * FROM '+t+' ORDER BY id')]for t in ['documents','document_contents','document_evidence_texts','financing_events','financing_event_evidence']}
            original=next(x for x in _build_transaction_items(c)if x['id']==self.target)['original_transaction_values']
            j=self.create(c);r=apply_transaction_value_correction(c,j,actor='test');self.assertEqual(r['status'],'applied')
            card=next(x for x in _build_transaction_items(c)if x['id']==self.target);self.assertEqual(card['effective_transaction_values']['effective_stages'],self.payload['proposed_value']);self.assertEqual(card['original_transaction_values'],original)
            for t,rows in raw.items():self.assertEqual(rows,[tuple(r)for r in c.execute('SELECT * FROM '+t+' ORDER BY id')])
    def test_legacy_single_source_contract_cannot_be_used_to_add_foreign_stage(self):
        self.case();p=copy.deepcopy(self.payload);p.pop('stage_evidence')
        with self.db.connect()as c:
            with self.assertRaises(ValueError):self.create(c,p)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_value_corrections').fetchone()[0],0)
    def test_bad_hash_or_unicode_span_is_rejected_before_any_apply(self):
        self.case()
        with self.db.connect()as c:
            for change in ['hash','span','coverage']:
                p=copy.deepcopy(self.payload)
                if change=='hash':p['stage_evidence'][1]['text_sha256']='0'*64
                elif change=='span':p['stage_evidence'][0]['start']+=1;p['stage_evidence'][0]['end']+=1
                else:p['stage_evidence'].pop()
                with self.subTest(change=change),self.assertRaises(ValueError):self.create(c,p)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_value_overrides').fetchone()[0],0)
    def test_same_issuer_other_round_source_is_not_accepted(self):
        self.case();doc,_=self.add('Company resolves on directed issue',body='Separate legal instrument is not rights issue registration.',date='2026-10-01T08:00:00Z')
        with self.db.connect()as c:
            p=copy.deepcopy(self.payload);p['stage_evidence'][1]['document_id']=doc;p['proposed_value'][1]['source_document_ids']=[doc]
            with self.assertRaisesRegex(ValueError,'stage_evidence_source_scope'):self.create(c,p)
    def test_changed_full_archive_outside_excerpt_blocks_apply_before_recalculation(self):
        self.case()
        with self.db.connect()as c:
            j=self.create(c);c.execute("UPDATE document_evidence_texts SET full_text=full_text||'changed' WHERE document_id=?",(self.docs[1],))
            with patch('capital_radar.radar_analysis.recalculate_adjusted_runway')as calculate:
                with self.assertRaisesRegex(ValueError,'stage_evidence_changed'):apply_transaction_value_correction(c,j,actor='test')
                calculate.assert_not_called()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_value_overrides').fetchone()[0],0)
    def test_duplicate_or_out_of_order_events_are_rejected(self):
        self.case()
        with self.db.connect()as c:
            for mode in ['duplicate','unordered']:
                p=copy.deepcopy(self.payload)
                if mode=='duplicate':p['proposed_value'].append(p['proposed_value'][0])
                else:p['proposed_value'].reverse()
                with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'stage_evidence_(duplicate_event|unordered_timeline)'):self.create(c,p)
    def test_valid_timeline_diagnostic_does_not_claim_apply_readiness(self):
        self.case()
        from capital_radar.stage_evidence import timeline_diagnostics
        result=timeline_diagnostics(self.payload['proposed_value'])
        self.assertTrue(result['timeline_valid'])
        self.assertFalse(result['complete_apply_readiness_verified'])
        result=timeline_diagnostics([{'private_source_text':'must never be echoed'}])
        self.assertEqual(result['current_validation_code'],'invalid_proposed_value')
        self.assertNotIn('private_source_text',json.dumps(result))
