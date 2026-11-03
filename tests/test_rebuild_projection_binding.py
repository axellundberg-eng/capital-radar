"""Audited rebuilds bind a financial identity, not arbitrary equal-key cards."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.projection_revision import read_projection_revision
from capital_radar.transaction_corrections import (
    GroupingReadbackMismatch, TransactionCorrectionError,
    apply_transaction_correction, approve_transaction_correction,
    create_transaction_correction, effective_document_rounds,
    revert_transaction_correction, transaction_correction_detail,
)
from capital_radar.transaction_feed import _build_transaction_items
from capital_radar.transaction_value_corrections import (
    apply_transaction_value_correction, approve_transaction_value_correction,
    create_transaction_value_correction, transaction_value_correction_detail,
)


DOCS = [22988, 22989, 24102, 24103, 24353, 25061, 25062]
TARGET = 'transaction:379:rights_issue:25061'


class RebuildProjectionBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'synthetic.db')
        self.db.initialize()
        self.old_key = 'biovica-rights-issue-2026'
        self.source = 'The outcome of the rights issue is SEK 29.2 million before costs. Payment is subject to ISP approval.'
        rows = [
            (22988, 'Biovica announces rights issue of approximately SEK 29.2 million', '2026-09-09', 'The board resolves on a rights issue of SEK 29.2 million.'),
            (22989, 'Biovica beslutar om företrädesemission om cirka 29,2 MSEK', '2026-09-09', 'Styrelsen beslutar om en företrädesemission om 29,2 MSEK.'),
            (24102, 'Biovica publishes memorandum regarding rights issue', '2026-10-06', 'The memorandum concerns the rights issue of SEK 29.2 million decided on September 9, 2026.'),
            (24103, 'Biovica offentliggör memorandum avseende företrädesemission', '2026-10-06', 'Memorandumet avser företrädesemissionen om 29,2 MSEK beslutad den 9 september 2026.'),
            (24353, 'Correction: Biovica clarifies the description of its patent position in the share issue memorandum published on Oct 6, 2026', '2026-10-12', 'The clarification concerns the patent description in the memorandum.'),
            (25061, 'Biovica announces outcome of rights issue', '2026-10-23', self.source),
            (25062, 'Biovica offentliggör utfall i företrädesemission', '2026-10-23', 'Utfallet i företrädesemissionen är 29,2 MSEK före kostnader. Betalning förutsätter ISP-godkännande.'),
        ]
        now = '2026-10-25'
        with self.db.connect() as c:
            c.execute("INSERT INTO issuers(id,canonical_name,normalized_name,identity_status,created_at,updated_at) VALUES(379,'Biovica','biovica','verified',?,?)", (now, now))
            for doc, title, date, text in rows:
                url = f'https://mfn.se/a/biovica/{doc}'
                c.execute("""INSERT INTO documents(id,issuer_id,event_key,canonical_url,title,published_at,language,profile_category,relevance_reason,created_at,updated_at)
                    VALUES(?,379,?,?,?,?,?,'financing','fixture',?,?)""", (doc, str(doc), url, title, date, 'sv' if doc in [22989,24103,25062] else 'en', now, now))
                c.execute("""INSERT INTO document_sources(document_id,source_id,source_external_id,discovered_url,source_published_at,first_seen_at,last_seen_at,metadata_json)
                    VALUES(?,'mfn',?,?,?,?,?,'{}')""", (doc, str(doc), url, date, now, now))
                c.execute("""INSERT INTO document_contents(document_id,source_url,extracted_text,text_sha256,char_count,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,'extracted',?,?)""", (doc, url, text, hashlib.sha256(text.encode()).hexdigest(), len(text), now, now))
            event = c.execute("""INSERT INTO financing_events(issuer_id,event_fingerprint,event_type,description,
                announcement_date,effective_date,status,cash_direction,gross_amount,cash_effect_amount,currency,
                liquidity_effect_kind,reflected_in_report,confidence,created_at,updated_at,financing_type,lifecycle_stage)
                VALUES(379,'outcome-raw','share_issue','Rights issue outcome','2026-10-23','2026-10-23',
                'completed','in',29200000,29200000,'SEK','confirmed_cash','no',.99,?,?,'rights_issue','outcome_published')""",(now,now)).lastrowid
            for doc in [25061,25062]:
                content = c.execute('SELECT id,extracted_text FROM document_contents WHERE document_id=?',(doc,)).fetchone()
                extraction = c.execute("""INSERT INTO extraction_jobs(issuer_id,document_id,content_id,provider,model,prompt_version,
                    input_sha256,selected_char_count,estimated_input_tokens,status,attempt_count,started_at,completed_at)
                    VALUES(379,?,?,'fixture','fixture','fixture',?,1,1,'completed',1,?,?)""",(doc,content['id'],str(doc),now,now)).lastrowid
                c.execute("""INSERT INTO financing_event_evidence(financing_event_id,document_id,extraction_job_id,evidence_label,source_excerpt,created_at)
                    VALUES(?,?,?,'fixture',?,?)""",(event,doc,extraction,content['extracted_text'],now))
            # A legacy audited identity on the older five sources reproduces
            # the production precondition without changing any source text.
            old = c.execute("""INSERT INTO financing_transaction_corrections(proposal_key,correction_type,transaction_id,issuer_id,expected_document_ids_json,resulting_rounds_json,reason,status,original_feed_json,created_by,created_at)
                VALUES('legacy','assign_document_to_round','transaction:379:rights_issue:22988',379,'[22988,22989,24102,24103,24353]',?,'legacy reviewed source grouping','applied','{}','fixture',?)""",
                (json.dumps([{'canonical_round_key': self.old_key, 'document_ids': DOCS[:5]}]), now)).lastrowid
            for doc in DOCS[:5]:
                c.execute("INSERT INTO financing_transaction_document_overrides(correction_id,issuer_id,document_id,canonical_round_key,created_at) VALUES(?,379,?,?,?)", (old,doc,self.old_key,now))

    def cards(self, c):
        return _build_transaction_items(c, issuer_ids=[379])

    def correction(self, c):
        payload = {'correction_type': 'rebuild_transaction', 'transaction_id': TARGET,
                   'issuer_id': 379, 'source_transaction_document_ids': [25061,25062],
                   'additional_document_ids': DOCS[:5], 'resulting_rounds': [
                       {'canonical_round_key': 'biovica-rights-issue-2026-09', 'document_ids': DOCS}],
                   'reason': 'All seven verified sources describe the same rights issue, including the neutral patent clarification.'}
        result = create_transaction_correction(c, payload, actor='fixture')
        approve_transaction_correction(c, result['id'], actor='fixture')
        return result['id']

    def cash(self, c, transaction_id=TARGET):
        card = next(i for i in self.cards(c) if i['id'] == transaction_id)
        doc = 25061 if transaction_id == TARGET else 22988
        source = self.source if doc == 25061 else 'The board resolves on a rights issue of SEK 29.2 million.'
        correction = create_transaction_value_correction(c, {
            'transaction_id': transaction_id, 'issuer_id':379,
            'field_name':'confirmed_cash_effect_amount',
            'expected_previous_value': card['effective_transaction_values']['confirmed_cash_effect_amount'],
            'proposed_value':0, 'source_document_id':doc, 'source_excerpt':source,
            'confidence':.99, 'rationale':'The source does not establish receipt of payment.'}, actor='fixture')
        approve_transaction_value_correction(c, correction['id'], actor='fixture')
        apply_transaction_value_correction(c, correction['id'], actor='fixture')
        return correction['id']

    def test_seven_sources_merge_with_stable_target_cash_provenance_and_exact_revert(self):
        with self.db.connect() as c:
            self.assertEqual(sorted(sorted(i['source_document_ids']) for i in self.cards(c)), [[22988,22989,24102,24103],[24353],[25061,25062]])
            self.assertEqual(next(i for i in self.cards(c) if i['id']==TARGET)['details']['confirmed_cash_effect_amount'],29200000)
            cash = self.cash(c)
            before = self.cards(c)
            original_events = [tuple(r) for r in c.execute('SELECT * FROM financing_events ORDER BY id')]
            source_rows = [tuple(r) for r in c.execute('SELECT * FROM document_contents ORDER BY id')]
            original = c.execute('SELECT original_transaction_json FROM financing_transaction_value_corrections WHERE id=?',(cash,)).fetchone()[0]
            correction = self.correction(c)
            revision = read_projection_revision(c)
            with patch('capital_radar.radar_analysis.current_analysis', return_value=[]):
                apply_transaction_correction(c, correction, actor='fixture')
            self.assertNotEqual(read_projection_revision(c),revision)
            merged = self.cards(c)
            self.assertEqual(len(merged),1)
            self.assertEqual(merged[0]['id'],TARGET)
            self.assertEqual(merged[0]['source_document_ids'],DOCS)
            self.assertEqual(merged[0]['details']['confirmed_cash_effect_amount'],0)
            self.assertIn(cash,merged[0]['applied_transaction_correction_ids'])
            self.assertEqual(merged[0]['grouping_projection_binding']['neutral_document_ids'],[24353])
            self.assertEqual(merged[0]['grouping_projection_binding']['correction_id'],correction)
            self.assertEqual(next(h for h in merged[0]['history'] if h['document_id']==24353)['status'],'related_source')
            self.assertTrue(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            readback = transaction_value_correction_detail(c,cash)
            self.assertEqual(readback['effective_transaction_values']['confirmed_cash_effect_amount'],0)
            self.assertIn(cash,readback['applied_transaction_correction_ids'])
            self.assertTrue(readback['projection_readback']['reported_runway_unchanged'])
            self.assertEqual(c.execute('SELECT original_transaction_json FROM financing_transaction_value_corrections WHERE id=?',(cash,)).fetchone()[0],original)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM document_contents ORDER BY id')],source_rows)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM financing_events ORDER BY id')],original_events)
            with patch('capital_radar.radar_analysis.current_analysis', return_value=[]):
                revert_transaction_correction(c,correction,actor='fixture')
            self.assertEqual(self.cards(c),before)

    def test_numeric_reported_runway_is_preserved_through_merge_and_revert(self):
        from capital_radar.radar_analysis import current_analysis
        with self.db.connect() as c:
            values = dict(issuer_id=379,as_of_date='2026-10-23',report_document_id=22988,
                report_period_end='2026-06-30',currency='SEK',baseline_cash=10_000_000,
                operating_cash_flow=-3_000_000,period_months=3,monthly_operating_burn=1_000_000,
                confirmed_post_report_adjustment=0,committed_liquidity=0,conditional_liquidity=0,
                adjusted_cash=10_000_000,weighted_liquidity=0,operating_runway_months=10,
                adjusted_runway_months=10,operating_need_score=50,financing_adjustment_points=0,
                adjusted_need_score=50,need_timing='6-12_months',alert_level='medium',evidence_posture='fixture',
                calculation_json='{}',calculation_sha256='synthetic-numeric',created_at='2026-10-23')
            c.execute('INSERT INTO runway_assessments ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
            self.cash(c)
            self.assertEqual(current_analysis(c,[379])[0]['reported_runway_months'],10)
            original = [tuple(row) for row in c.execute('SELECT * FROM runway_assessments')]
            correction = self.correction(c)
            apply_transaction_correction(c,correction,actor='fixture')
            self.assertEqual(current_analysis(c,[379])[0]['reported_runway_months'],10)
            self.assertEqual(current_analysis(c,[379])[0]['adjusted_runway_months'],10)
            self.assertEqual([tuple(row) for row in c.execute('SELECT * FROM runway_assessments')],original)
            revert_transaction_correction(c,correction,actor='fixture')
            self.assertEqual(current_analysis(c,[379])[0]['reported_runway_months'],10)
            self.assertEqual([tuple(row) for row in c.execute('SELECT * FROM runway_assessments')],original)

    def test_binding_change_alone_advances_revision_and_rollback_restores_it(self):
        with self.db.connect() as c:
            correction = self.correction(c)
            apply_transaction_correction(c,correction,actor='fixture')
            revision = read_projection_revision(c)
            c.execute('SAVEPOINT revision_probe')
            c.execute('UPDATE financing_transaction_projection_bindings SET active=0 WHERE correction_id=?',(correction,))
            self.assertNotEqual(read_projection_revision(c),revision)
            c.execute('ROLLBACK TO SAVEPOINT revision_probe')
            c.execute('RELEASE SAVEPOINT revision_probe')
            self.assertEqual(read_projection_revision(c),revision)

    def test_exact_card_inventory_is_checked_without_replacing_any_existing_key(self):
        with self.db.connect() as c:
            payload = {'correction_type':'rebuild_transaction','transaction_id':TARGET,
                       'issuer_id':379,'source_transaction_document_ids':[25061,25062],
                       'additional_document_ids':DOCS[:5], 'resulting_rounds':[{'canonical_round_key':self.old_key,'document_ids':DOCS}],
                       'reason':'Fresh expected inventory is mandatory even when all existing assigned keys are retained.'}
            correction = create_transaction_correction(c,payload,actor='fixture')
            approve_transaction_correction(c,correction['id'],actor='fixture')
            c.execute("UPDATE documents SET title='Biovica publishes terms for warrants TO3' WHERE id=24353")
            with self.assertRaisesRegex(TransactionCorrectionError,'expected_state_conflict'):
                apply_transaction_correction(c,correction['id'],actor='fixture')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_projection_bindings').fetchone()[0],0)

    def test_projection_failure_rolls_back_binding_and_revision_before_recalculation(self):
        from capital_radar.transaction_corrections import _affected_feed_items
        with self.db.connect() as c:
            self.cash(c)
            before = self.cards(c)
            correction = self.correction(c)
            revision = read_projection_revision(c)
            calls = 0
            def duplicate(*args, **kwargs):
                nonlocal calls
                cards = _affected_feed_items(*args,**kwargs)
                calls += 1
                return [*cards,dict(cards[0])] if calls == 2 else cards
            with patch('capital_radar.transaction_corrections._affected_feed_items',side_effect=duplicate), patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalc:
                with self.assertRaises(GroupingReadbackMismatch):
                    apply_transaction_correction(c,correction,actor='fixture')
                recalc.assert_not_called()
            self.assertEqual(self.cards(c),before)
            self.assertEqual(read_projection_revision(c),revision)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_projection_bindings').fetchone()[0],0)

    def test_changed_neutral_source_never_remains_verified(self):
        for mutation in ('title','text','event','link','same_category','missing_content'):
            with self.subTest(mutation=mutation), self.db.connect() as c:
                c.execute('SAVEPOINT isolated_mutation')
                correction = self.correction(c)
                apply_transaction_correction(c,correction,actor='fixture')
                if mutation == 'missing_content':
                    c.execute('DELETE FROM document_contents WHERE document_id=24353')
                elif mutation == 'same_category':
                    c.execute("UPDATE documents SET title='Biovica announces rights issue of SEK 1 million' WHERE id=24353")
                elif mutation == 'title':
                    c.execute("UPDATE documents SET title='Biovica announces warrants TO2' WHERE id=24353")
                elif mutation == 'text':
                    c.execute("UPDATE document_contents SET extracted_text='Exercise of warrants TO2 provides SEK1million.' WHERE document_id=24353")
                elif mutation == 'event':
                    evidence = c.execute('SELECT * FROM financing_event_evidence LIMIT 1').fetchone()
                    c.execute("INSERT INTO financing_event_evidence(financing_event_id,document_id,extraction_job_id,evidence_label,source_excerpt,created_at) VALUES(?,24353,?,'fixture','New independent event','2026-10-25')",(evidence['financing_event_id'],evidence['extraction_job_id']))
                else:
                    # This synthetic active relation is deliberately independent.
                    value = self.cash(c)
                    c.execute("INSERT INTO financing_transaction_document_links(correction_id,issuer_id,document_id,canonical_round_key,category,transaction_id,created_at) VALUES(?,379,24353,'separate-to2','warrant_exercise','transaction:379:warrant_exercise:24353','2026-10-25')",(value,))
                readback = transaction_correction_detail(c,correction)
                self.assertFalse(readback['current_round_matches_applied'])
                core = next(card for card in self.cards(c) if card['id']==TARGET)
                self.assertNotIn(24353,core['source_document_ids'])
                self.assertEqual(core['grouping_projection_binding']['changed_neutral_document_ids'],[24353])
                revert_transaction_correction(c,correction,actor='fixture')
                self.assertEqual(c.execute('SELECT status FROM financing_transaction_corrections WHERE id=?',(correction,)).fetchone()[0],'reverted')
                c.execute('ROLLBACK TO SAVEPOINT isolated_mutation')
                c.execute('RELEASE SAVEPOINT isolated_mutation')

    def test_retired_scalar_and_unvalued_separate_security_are_refused(self):
        with self.db.connect() as c:
            self.cash(c,'transaction:379:rights_issue:22988')
            correction = self.correction(c)
            with patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalc:
                with self.assertRaisesRegex(TransactionCorrectionError,'retired_value_override'):
                    apply_transaction_correction(c,correction,actor='fixture')
                recalc.assert_not_called()
            self.assertEqual(len(self.cards(c)),3)

    def test_unknown_equity_headline_is_not_neutral(self):
        with self.db.connect() as c:
            c.execute("UPDATE documents SET title='Biovica announces share issue to guarantors' WHERE id=24353")
            correction = self.correction(c)
            with self.assertRaisesRegex(TransactionCorrectionError,'separate_instrument'):
                apply_transaction_correction(c,correction,actor='fixture')

    def test_rebuild_of_to2_and_to3_cannot_hide_series_behind_one_audited_key(self):
        with self.db.connect() as c:
            c.execute("UPDATE documents SET title='Biovica publishes terms for warrants TO3' WHERE id IN (22988,22989,24102,24103)")
            c.execute("UPDATE documents SET title='Biovica publishes outcome for warrants TO2' WHERE id IN (25061,25062)")
            c.execute("UPDATE financing_events SET financing_type='warrant_exercise'")
            payload = {'correction_type':'rebuild_transaction','transaction_id':TARGET.replace('rights_issue','warrant_exercise'),
                       'issuer_id':379,'source_transaction_document_ids':[25061,25062],
                       'additional_document_ids':DOCS[:5], 'resulting_rounds':[{'canonical_round_key':'bad-shared-options-key','document_ids':DOCS}],
                       'reason':'This requested merge must reject distinct legal TO2 and TO3 series.'}
            correction = create_transaction_correction(c,payload,actor='fixture')
            approve_transaction_correction(c,correction['id'],actor='fixture')
            with self.assertRaisesRegex(TransactionCorrectionError,'instrument_series_conflict'):
                apply_transaction_correction(c,correction['id'],actor='fixture')

    def test_options_are_not_reclassified_into_rights_round(self):
        with self.db.connect() as c:
            c.execute("UPDATE documents SET title='Biovica publishes terms for warrants TO2' WHERE id=24353")
            correction = self.correction(c)
            with self.assertRaisesRegex(TransactionCorrectionError,'separate_instrument'):
                apply_transaction_correction(c,correction,actor='fixture')


if __name__ == '__main__':
    unittest.main()
