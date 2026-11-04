"""Shared source, separate convertible and warrant; no production data."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.evidence_store import archive_document_evidence
from capital_radar.transaction_feed import _build_transaction_items
from capital_radar.transaction_corrections import (create_transaction_correction,
    approve_transaction_correction, apply_transaction_correction, revert_transaction_correction,
    transaction_correction_detail, TransactionCorrectionError, GroupingReadbackMismatch)
from capital_radar.transaction_value_corrections import (create_transaction_value_correction,
    approve_transaction_value_correction, apply_transaction_value_correction)

TARGET = 'transaction:1:warrant_exercise:1'
KEY = 'fixture-warrants-2026'


class InstrumentScopedGroupingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name)/'synthetic.db'); self.db.initialize()
        self.texts = {1:'The company resolves on a conditional directed issue of convertibles and warrants. The convertible limit is SEK 20 million. Warrant potential proceeds were SEK 4.5 million.',
                      2:'Correction: full exercise of the warrants may provide SEK 16.5 million, not SEK 4.5 million. SEK 4.5 million is share capital. No warrants have been exercised.'}
        titles = {1:'Company resolves on directed issue of convertibles and warrants',
                  2:'Correction: Incorrect potential proceeds from warrants in previous press release'}
        with self.db.connect() as c:
            c.execute("INSERT INTO issuers(id,canonical_name,normalized_name,identity_status,created_at,updated_at) VALUES(1,'Company','company','verified','2026-10-01','2026-10-01')")
            for doc,text in self.texts.items():
                url=f'https://mfn.se/a/fixture/{doc}';date=f'2026-10-0{doc}'
                c.execute("""INSERT INTO documents(id,issuer_id,event_key,canonical_url,title,published_at,language,profile_category,relevance_reason,created_at,updated_at)
                    VALUES(?,1,?,?,?,?,'en','financing','fixture',?,?)""",(doc,str(doc),url,titles[doc],date,date,date))
                content=c.execute("""INSERT INTO document_contents(document_id,source_url,extracted_text,text_sha256,char_count,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,'extracted',?,?)""",(doc,url,text,hashlib.sha256(text.encode()).hexdigest(),len(text),date,date)).lastrowid
                archive_document_evidence(c,document_id=doc,content_id=content,text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),page_count=None)
                for category,amount in ([('convertible',20_000_000),('warrant_exercise',4_500_000)] if doc==1 else [('warrant_exercise',4_500_000)]):
                    event=c.execute("""INSERT INTO financing_events(issuer_id,event_fingerprint,event_type,description,
                        announcement_date,effective_date,status,cash_direction,gross_amount,cash_effect_amount,currency,
                        liquidity_effect_kind,reflected_in_report,confidence,created_at,updated_at,financing_type,canonical_round_key)
                        VALUES(1,?,?,?, ?,?,'conditional','in',?,0,'SEK','conditional','no',.99,?,?,?,?)""",
                        (f'{doc}-{category}',category,text,date,date,amount,date,date,category,'fixture-bundle' if doc==1 else None)).lastrowid
                    extraction=c.execute("""INSERT INTO extraction_jobs(issuer_id,document_id,content_id,provider,model,prompt_version,
                        input_sha256,selected_char_count,estimated_input_tokens,status,attempt_count,started_at,completed_at)
                        VALUES(1,?,?,'fixture','fixture','fixture',?,1,1,'completed',1,?,?)""",(doc,content,str(event),date,date)).lastrowid
                    c.execute("""INSERT INTO financing_event_evidence(financing_event_id,document_id,extraction_job_id,evidence_label,source_excerpt,created_at)
                        VALUES(?,?,?,'fixture',?,?)""",(event,doc,extraction,text,date))

    def cards(self,c): return _build_transaction_items(c,issuer_ids=[1])

    def value(self,c,tx,doc,field,value):
        card=next(x for x in self.cards(c) if x['id']==tx)
        r=create_transaction_value_correction(c,dict(transaction_id=tx,issuer_id=1,
            field_name=field,expected_previous_value=card['effective_transaction_values'][field],
            proposed_value=value,source_document_id=doc,source_excerpt=self.texts[doc],confidence=.99,
            rationale='Synthetic verified source and exact current effective value.'),actor='fixture')
        approve_transaction_value_correction(c,r['id'],actor='fixture')
        with patch('capital_radar.radar_analysis.current_analysis',return_value=[]):
            apply_transaction_value_correction(c,r['id'],actor='fixture')
        return r['id']

    def correction(self,c):
        return create_transaction_correction(c,dict(correction_type='rebuild_transaction',
            instrument_category='warrant_exercise',transaction_id=TARGET,issuer_id=1,
            source_transaction_document_ids=[1],additional_document_ids=[2],
            resulting_rounds=[dict(canonical_round_key=KEY,document_ids=[1,2])],
            reason='Same unexercised warrant instrument; preserve the separate convertible.'),actor='fixture')['id']

    def apply(self,c,n):
        approve_transaction_correction(c,n,actor='fixture')
        with patch('capital_radar.radar_analysis.current_analysis',return_value=[]):
            return apply_transaction_correction(c,n,actor='fixture')

    def test_merge_preserves_convertible_values_original_rows_and_reverts(self):
        with self.db.connect() as c:
            self.value(c,TARGET,1,'amount_basis','maximum_proceeds')
            self.value(c,'transaction:1:convertible:1',1,'amount',20_000_001)
            before=self.cards(c); self.assertEqual(len(before),3)
            originals=[tuple(x) for x in c.execute('SELECT * FROM financing_events ORDER BY id')]
            n=self.correction(c);self.apply(c,n);after=self.cards(c)
            self.assertEqual(len(after),2)
            warrant=next(x for x in after if x['id']==TARGET)
            self.assertEqual(warrant['source_document_ids'],[1,2])
            self.assertEqual(warrant['effective_transaction_values']['canonical_round_key'],KEY)
            self.assertEqual(warrant['effective_transaction_values']['amount_basis'],'maximum_proceeds')
            self.assertEqual(next(x for x in before if x['category']=='convertible'),next(x for x in after if x['category']=='convertible'))
            self.assertEqual(originals,[tuple(x) for x in c.execute('SELECT * FROM financing_events ORDER BY id')])
            detail=transaction_correction_detail(c,n)
            self.assertTrue(detail['grouping_matches_applied']);self.assertTrue(detail['preserved_instruments_match'])
            self.assertEqual(len(detail['current_round_value']),2)
            with patch('capital_radar.radar_analysis.current_analysis',return_value=[]):revert_transaction_correction(c,n,actor='fixture')
            self.assertEqual(self.cards(c),before)

    def test_retired_value_owner_is_rejected_without_losing_corrections(self):
        with self.db.connect() as c:
            self.value(c,'transaction:1:warrant_exercise:2',2,'amount',16_500_000)
            n=self.correction(c);before=self.cards(c)
            with self.assertRaisesRegex(TransactionCorrectionError,'instrument_grouping_retired_value_override'):self.apply(c,n)
            self.assertEqual(self.cards(c),before)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_instrument_bindings').fetchone()[0],0)

    def test_duplicate_projection_rolls_back_before_calculation(self):
        import capital_radar.transaction_corrections as grouping
        original=grouping._affected_feed_items
        with self.db.connect() as c:
            n=self.correction(c);before=self.cards(c)
            def duplicate(*args,**kwargs):
                cards=original(*args,**kwargs)
                if args[0].execute('SELECT COUNT(*) FROM financing_transaction_instrument_bindings').fetchone()[0]:
                    cards.append(next(x for x in cards if x['id']==TARGET))
                return cards
            with patch.object(grouping,'_affected_feed_items',side_effect=duplicate),patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as calculate:
                with self.assertRaises(GroupingReadbackMismatch):self.apply(c,n)
                calculate.assert_not_called()
            self.assertEqual(self.cards(c),before)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_instrument_bindings').fetchone()[0],0)

    def test_changed_archived_source_invalidates_membership(self):
        with self.db.connect() as c:
            n=self.correction(c);self.apply(c,n)
            # Only the immutable archive changes; display content remains unchanged.
            archive_document_evidence(c,document_id=2,content_id=None,text=self.texts[2]+' Changed.',
                text_sha256=hashlib.sha256((self.texts[2]+' Changed.').encode()).hexdigest(),page_count=None)
            detail=transaction_correction_detail(c,n)
            self.assertFalse(detail['grouping_matches_applied']);self.assertFalse(detail['instrument_sources_unchanged'])
            self.assertEqual(len(self.cards(c)),3)

    def test_updated_source_correction_depends_on_group_membership(self):
        with self.db.connect() as c:
            n=self.correction(c);self.apply(c,n)
            self.value(c,TARGET,2,'amount',16_500_000)
            self.assertTrue(transaction_correction_detail(c,n)['grouping_matches_applied'])
            with self.assertRaisesRegex(TransactionCorrectionError,'instrument_grouping_dependent_value_override'):
                revert_transaction_correction(c,n,actor='fixture')

    def test_older_notice_cannot_overwrite_newer_official_amount_rectification(self):
        from capital_radar.transaction_value_corrections import SourceChronologyError
        with self.db.connect() as c:
            n=self.correction(c);self.apply(c,n)
            self.value(c,TARGET,2,'amount',16_500_000)
            before=self.cards(c)
            with self.assertRaises(SourceChronologyError):self.value(c,TARGET,1,'amount',4_500_000)
            self.assertEqual(self.cards(c),before)

    def test_other_instrument_value_change_is_atomic_failure(self):
        import copy
        import capital_radar.transaction_corrections as grouping
        original=grouping._affected_feed_items
        with self.db.connect() as c:
            n=self.correction(c);before=self.cards(c)
            def changed(*args,**kwargs):
                cards=copy.deepcopy(original(*args,**kwargs))
                if args[0].execute('SELECT COUNT(*) FROM financing_transaction_instrument_bindings').fetchone()[0]:
                    next(x for x in cards if x['category']=='convertible')['effective_transaction_values']['amount']=1
                return cards
            with patch.object(grouping,'_affected_feed_items',side_effect=changed),patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as calculate:
                with self.assertRaisesRegex(TransactionCorrectionError,'instrument_grouping_other_instrument_changed'):self.apply(c,n)
                calculate.assert_not_called()
            self.assertEqual(self.cards(c),before)

    def test_received_cash_and_different_series_are_not_merged(self):
        with self.db.connect() as c:
            self.value(c,TARGET,1,'confirmed_cash_effect_amount',1)
            n=self.correction(c)
            with self.assertRaisesRegex(TransactionCorrectionError,'instrument_grouping_separate_instrument'):self.apply(c,n)
        with self.db.connect() as c:
            self.value(c,TARGET,1,'confirmed_cash_effect_amount',0)
            c.execute("UPDATE documents SET title='Company resolves on convertibles and warrants TO2' WHERE id=1")
            c.execute("UPDATE documents SET title='Correction: proceeds from warrants TO3' WHERE id=2")
            n=self.correction(c)
            with self.assertRaisesRegex(TransactionCorrectionError,'instrument_grouping_separate_instrument'):self.apply(c,n)



if __name__=='__main__':unittest.main()
