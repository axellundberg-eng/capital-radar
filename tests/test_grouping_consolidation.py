"""Regression: three fragmented rights owners plus separate fee → two rounds."""
import copy
import json
import unittest
from unittest.mock import patch
import test_transaction_feed as fixtures
from capital_radar.transaction_feed import _build_transaction_items
from capital_radar.transaction_corrections import (create_transaction_correction,approve_transaction_correction,
    apply_transaction_correction,revert_transaction_correction,TransactionCorrectionError,reviewed_grouping_readback)
from capital_radar.transaction_value_corrections import (create_transaction_value_correction,
    approve_transaction_value_correction,apply_transaction_value_correction)
from capital_radar.projection_revision import read_projection_revision

class ConsolidationTests(unittest.TestCase):
    setUp=fixtures.TransactionFeedTests.setUp
    add=fixtures.TransactionFeedTests.add
    add_event=fixtures.TransactionFeedTests.add_event
    def cards(self,c):return _build_transaction_items(c)
    def scalar(self,c,owner,field,value,source):
        card=next(x for x in self.cards(c)if x['id']==owner)
        d=create_transaction_value_correction(c,{'transaction_id':owner,'issuer_id':card['issuer_id'],
            'field_name':field,'expected_previous_value':card['effective_transaction_values'][field],
            'proposed_value':value,'source_document_id':source,'source_excerpt':self.text,
            'confidence':.99,'rationale':'Keep the source-owned scalar and immutable original evidence.'},actor='test')
        approve_transaction_value_correction(c,d['id'],actor='test');apply_transaction_value_correction(c,d['id'],actor='test');return d['id']
    def case(self):
        self.text='The rights issue provides SEK 57.5 million before costs. Guarantee shares are paid by set-off, not cash.'
        self.docs=[]
        for i,key in enumerate(['sv-original','en-original','terms-round','terms-round','fee-original']):
            d,_=self.add('Company announces outcome of rights issue',body=self.text,date=f'2026-09-{i+1:02}T08:00:00Z',language='sv'if i%2==0 else'en')
            self.add_event(d,round_key=key,stage='outcome_published',gross_amount=57500000,cash_effect_amount=57500000)
            self.docs.append(d)
        with self.db.connect()as c:
            cards=self.cards(c);self.assertEqual(len(cards),4)
            bydoc=lambda d:next(x['id']for x in cards if d in x['source_document_ids'])
            self.owner1=bydoc(self.docs[0]);self.owner2=bydoc(self.docs[1]);self.target=bydoc(self.docs[2]);self.fee=bydoc(self.docs[4])
            self.ids=[self.scalar(c,self.owner1,'confirmed_cash_effect_amount',0,self.docs[0]),self.scalar(c,self.owner2,'amount',57500000,self.docs[1]),self.scalar(c,self.fee,'category','directed_issue',self.docs[4]),self.scalar(c,self.fee,'amount',900000,self.docs[4])]
            # Archived text exists separately and is pinned by full hash.
            from capital_radar.evidence_store import ensure_document_evidence
            for d in self.docs:ensure_document_evidence(c,d)
            # Prior reviewed assignments must be replaced atomically, not bypassed.
            old=c.execute("INSERT INTO financing_transaction_corrections(proposal_key,correction_type,transaction_id,issuer_id,expected_document_ids_json,resulting_rounds_json,reason,status,original_feed_json,created_by,created_at) VALUES('legacy','assign_document_to_round',?,?,'[]','[]','legacy grouping','applied','{}','test','2026-09-01')",(self.owner1,cards[0]['issuer_id'])).lastrowid
            for d,key in zip(self.docs,['sv-original','en-original','terms-round','terms-round','fee-original']):
                c.execute('INSERT INTO financing_transaction_document_overrides(correction_id,issuer_id,document_id,canonical_round_key,created_at)VALUES(?,?,?,?,?)',(old,cards[0]['issuer_id'],d,key,'2026-09-01'))
            self.rounds=[{'canonical_round_key':'rights-canonical','document_ids':self.docs[:3]}, {'canonical_round_key':'fee-canonical','document_ids':self.docs[3:]}]
            card=next(x for x in self.cards(c)if x['id']==self.target)
            p={'correction_type':'split_transaction','transaction_id':self.target,'issuer_id':card['issuer_id'],
                'source_transaction_document_ids':card['source_document_ids'],'additional_document_ids':[d for d in self.docs if d not in card['source_document_ids']],
                'resulting_rounds':self.rounds,'reason':'Consolidate three rights owners while keeping fee instrument separate.'}
            g=create_transaction_correction(c,p,actor='test');approve_transaction_correction(c,g['id'],actor='test');self.group=g['id']
    def protected(self,c):
        return {t:[tuple(x)for x in c.execute('SELECT * FROM '+t+' ORDER BY id')]for t in ['documents','document_contents','document_evidence_texts','financing_events','financing_event_evidence','financing_transaction_value_corrections','financing_transaction_value_overrides']}
    def test_consolidate_exact_membership_scalar_provenance_and_revert(self):
        self.case()
        with self.db.connect()as c:
            before=self.cards(c);raw=self.protected(c)
            apply_transaction_correction(c,self.group,actor='test')
            cards=self.cards(c);self.assertEqual(len(cards),2);main=next(x for x in cards if x['id']==self.target);fee=next(x for x in cards if x['id']==self.fee)
            self.assertEqual(main['source_document_ids'],self.docs[:3]);self.assertEqual(fee['source_document_ids'],self.docs[3:]);self.assertEqual(fee['category'],'directed_issue')
            self.assertEqual(main['amount'],57500000);self.assertEqual(main['details']['confirmed_cash_effect_amount'],0)
            self.assertTrue(set(self.ids[:2]).issubset(main['applied_transaction_correction_ids']));self.assertTrue(set(self.ids[2:]).issubset(fee['applied_transaction_correction_ids']))
            self.assertTrue(reviewed_grouping_readback(c,self.group,self.rounds,cards)['effective_matches_applied']);self.assertEqual(raw,self.protected(c))
            revert_transaction_correction(c,self.group,actor='test');self.assertEqual(before,self.cards(c));self.assertEqual(raw,self.protected(c))
    def reject(self,c,code):
        before=self.cards(c);raw=self.protected(c);revision=read_projection_revision(c)
        with patch('capital_radar.radar_analysis.recalculate_adjusted_runway')as calc:
            with self.assertRaisesRegex(TransactionCorrectionError,code):apply_transaction_correction(c,self.group,actor='test')
            calc.assert_not_called()
        self.assertEqual(self.cards(c),before);self.assertEqual(raw,self.protected(c));self.assertEqual(revision,read_projection_revision(c));self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_grouping_consolidations').fetchone()[0],0)
    def test_conflicting_field_owners_rejected_even_when_equal(self):
        self.case()
        with self.db.connect()as c:
            self.scalar(c,self.owner1,'amount',57500000,self.docs[0]);self.reject(c,'grouping_consolidation_field_conflict')
    def test_duplicate_projection_rolls_back_before_calculation(self):
        import capital_radar.transaction_corrections as grouping
        self.case()
        with self.db.connect()as c:
            original=grouping._affected_feed_items
            def duplicate(conn,*args,**kw):
                result=original(conn,*args,**kw)
                if conn.execute('SELECT 1 FROM financing_transaction_grouping_consolidations').fetchone():result.append(copy.deepcopy(result[0]))
                return result
            with patch.object(grouping,'_affected_feed_items',side_effect=duplicate):self.reject(c,'grouping_readback_mismatch')
    def test_archive_change_invalidates_cached_readback(self):
        self.case()
        with self.db.connect()as c:
            apply_transaction_correction(c,self.group,actor='test');cached=self.cards(c)
            c.execute("UPDATE document_evidence_texts SET full_text=full_text||' changed' WHERE document_id=?",(self.docs[0],))
            self.assertFalse(reviewed_grouping_readback(c,self.group,self.rounds,cached)['effective_matches_applied'])
    def test_later_target_scalar_supersedes_with_new_audit_and_blocks_revert(self):
        self.case()
        with self.db.connect()as c:
            apply_transaction_correction(c,self.group,actor='test')
            new=self.scalar(c,self.target,'amount',58000000,self.docs[2]);main=next(x for x in self.cards(c)if x['id']==self.target)
            self.assertEqual(main['amount'],58000000);self.assertIn(new,main['applied_transaction_correction_ids']);self.assertNotIn(self.ids[1],main['applied_transaction_correction_ids'])
            self.assertIsNotNone(c.execute('SELECT 1 FROM financing_transaction_value_overrides WHERE correction_id=? AND active=1',(self.ids[1],)).fetchone())
            with self.assertRaisesRegex(TransactionCorrectionError,'grouping_consolidation_dependent_value_override'):revert_transaction_correction(c,self.group,actor='test')
    def test_changed_inherited_journal_never_passes_cached_readback(self):
        self.case()
        with self.db.connect()as c:
            apply_transaction_correction(c,self.group,actor='test');cached=self.cards(c)
            c.execute('UPDATE financing_transaction_value_overrides SET active=0 WHERE correction_id=?',(self.ids[0],))
            self.assertFalse(reviewed_grouping_readback(c,self.group,self.rounds,cached)['effective_matches_applied'])
    def test_structured_value_owner_is_not_migrated(self):
        self.case()
        with self.db.connect()as c:
            self.scalar(c,self.owner1,'effective_stages',[{'lifecycle_stage':'announced','effective_at':'2026-09-01','source_document_ids':[self.docs[0]],'reported_values':{'gross_amount':57500000}}],self.docs[0])
            self.reject(c,'grouping_consolidation_structured_values')
