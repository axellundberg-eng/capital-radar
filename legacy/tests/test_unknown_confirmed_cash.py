"""Reviewed unknown cash must never turn into gross proceeds or a fake zero."""
import copy
import os
import unittest
from unittest.mock import patch
import test_transaction_value_corrections as fixtures
from capital_radar.transaction_value_corrections import (
    _validate_proposed, TransactionValueCorrectionError,
    create_transaction_value_correction, approve_transaction_value_correction,
    apply_transaction_value_correction, revert_transaction_value_correction,
)
from capital_radar.transaction_feed import _build_transaction_items, get_transaction_detail
from capital_radar.radar_analysis import calculate, current_analysis, UNKNOWN_CASH_REASON
from capital_radar.transaction_read_model import TransactionReadModel

class UnknownCashTests(unittest.TestCase):
    setUp = fixtures.TransactionValueCorrectionTests.setUp

    def initial(self):
        with self.db.connect() as c:
            return _build_transaction_items(c, issuer_ids=[self.issuer_id])[0]

    def payload(self, old, value=None):
        return dict(transaction_id=self.initial()['id'], issuer_id=self.issuer_id,
            field_name='confirmed_cash_effect_amount', expected_previous_value=old,
            proposed_value=value, source_document_id=self.document_id,
            source_excerpt=self.source, confidence=.99,
            rationale='Gross proceeds before costs do not prove actual cash receipt for this round.')

    def direct(self, c, old, value=None):
        p=self.payload(old,value);j=create_transaction_value_correction(c,p,actor='review-agent')
        approve_transaction_value_correction(c,j['id'],actor='review-agent')
        apply_transaction_value_correction(c,j['id'],actor='review-agent');return j['id']

    def test_scalar_unknown_and_known_zero_are_distinct_and_structured_cash_is_required(self):
        self.assertIsNone(_validate_proposed('confirmed_cash_effect_amount',None))
        self.assertEqual(_validate_proposed('confirmed_cash_effect_amount',0),0)
        for value in [None,True,-1,float('nan'),float('inf'),'0']:
            with self.subTest(value=value),self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed('confirmed_cash_effect_amount',value,allow_unknown=False)
        instrument=dict(canonical_round_key='warrants',category='warrant_exercise',
            status='announced',lifecycle_stage='announced',amount=30,amount_basis='maximum_proceeds',
            currency='SEK',confirmed_cash_effect_amount=None,source_document_ids=[self.document_id])
        with self.assertRaises(TransactionValueCorrectionError):
            _validate_proposed('additional_instruments',[instrument])

    def test_unknown_blocks_stale_gross_cash_noncash_text_credit_and_future_fallbacks(self):
        assessment=dict(issuer_id=1,baseline_cash=60,monthly_operating_burn=10,
            operating_cash_flow=-30,operating_runway_months=6,report_period_end='2026-06-30',currency='SEK')
        base=dict(id='transaction:1:rights_issue:3',issuer_id=1,category='rights_issue',
            status='completed',currency='SEK',amount=30,amount_basis='outcome_gross',
            published_at='2026-07-15',history=[dict(published_at='2026-07-15',title='Rights issue')],
            confirmed_cash_effect_amount=30,source_document_ids=[3],
            details=dict(confirmed_cash_effect_amount=30,outcome_amount=30,announced_amount=30,
                financing_dossier={'drawn_amount':30}),
            original_transaction_values={'confirmed_cash_effect_amount':30},
            effective_transaction_values={'confirmed_cash_effect_amount':None},
            effective_corrected_fields=['confirmed_cash_effect_amount'])
        for fields in [{},{'_source_text':'39.9 million includes debt setoff and kvittning'},
                       {'category':'credit_facility'},{'amount_basis':'conditional_liquidity'},
                       {'category':'warrant_exercise','status':'announced'}]:
            tx={**copy.deepcopy(base),**fields}
            result=calculate(assessment,[tx],{},as_of='2026-08-31');event=result['events'][0]
            self.assertIsNone(event['amount']);self.assertIsNone(event['cash_effect'])
            self.assertEqual(event['exclusion_reason'],UNKNOWN_CASH_REASON)
            self.assertEqual(result['pro_forma_cash_adjustment'],0)
            self.assertEqual(result['adjusted_runway_months'],result['runway_from_today_months'])
            self.assertEqual(result['reported_runway_months'],6)
            self.assertIn(UNKNOWN_CASH_REASON,result['pro_forma_comment'])

    def test_unreviewed_null_legacy_gross_and_known_cash_keep_existing_semantics(self):
        from capital_radar.radar_analysis import _cash_amount
        tx=dict(category='rights_issue',status='completed',amount=30,amount_basis='outcome_gross')
        self.assertEqual(_cash_amount(tx,{},'')[:1],(30,))
        tx['effective_transaction_values']={'confirmed_cash_effect_amount':None}
        tx['original_transaction_values']={'confirmed_cash_effect_amount':None}
        self.assertEqual(_cash_amount(tx,{},'')[:1],(30,))
        tx['effective_corrected_fields']=['confirmed_cash_effect_amount']
        for value in [0,12]:
            tx['effective_transaction_values']['confirmed_cash_effect_amount']=value
            self.assertEqual(_cash_amount(tx,{'confirmed_cash_effect_amount':30},'kvittning'),
                (value,'confirmed_cash_effect_amount',None))

    def test_audited_null_projects_every_view_and_preserves_originals_and_reported(self):
        card=self.initial()
        with self.db.connect() as c:
            before={table:[tuple(row)for row in c.execute('SELECT * FROM '+table)]
                for table in ['documents','document_contents','financing_events','runway_assessments']}
            j=self.direct(c,card['effective_transaction_values']['confirmed_cash_effect_amount'])
            after=get_transaction_detail(c,card['id']);d=after['details'];v=after['effective_transaction_values']
            self.assertIsNone(v['confirmed_cash_effect_amount'])
            self.assertIsNone(d['confirmed_cash_effect_amount'])
            self.assertIsNone(d['financing_dossier']['confirmed_cash_effect_amount'])
            fact=d['financing_dossier']['round_reconciliation']['actual_outcome']['confirmed_cash_effect_amount']
            self.assertIsNone(fact['value']);self.assertEqual(fact['correction_id'],j)
            self.assertEqual(fact['source_document_ids'],[self.document_id])
            self.assertEqual(after['original_transaction_values'],card['original_transaction_values'])
            self.assertEqual(after['amount'],card['amount']);self.assertEqual(after['source_document_ids'],card['source_document_ids'])
            a=current_analysis(c,[self.issuer_id])[0];self.assertEqual(a['reported_runway_months'],10)
            self.assertIsNone(a['events'][0]['cash_effect'])
            for table,rows in before.items():self.assertEqual(rows,[tuple(row)for row in c.execute('SELECT * FROM '+table)])
            revert_transaction_value_correction(c,j,actor='review-agent')
            restored=get_transaction_detail(c,card['id'])
            self.assertEqual(restored['effective_transaction_values']['confirmed_cash_effect_amount'],card['effective_transaction_values']['confirmed_cash_effect_amount'])

    def test_published_projection_keeps_null_cash_in_feed_and_analysis(self):
        card=self.initial()
        with self.db.connect() as c:self.direct(c,card['effective_transaction_values']['confirmed_cash_effect_amount'])
        model=TransactionReadModel(self.db.path);self.assertTrue(model.refresh_once())
        import sqlite3,json
        from contextlib import closing
        with closing(sqlite3.connect(model.path)) as c:
            published=json.loads(c.execute('SELECT payload FROM projection_transactions WHERE id=?',(card['id'],)).fetchone()[0])
            self.assertIsNone(published['details']['confirmed_cash_effect_amount'])
            self.assertIsNone(published['details']['financing_dossier']['confirmed_cash_effect_amount'])
            a=json.loads(c.execute('SELECT payload FROM projection_analyses WHERE issuer_id=?',(self.issuer_id,)).fetchone()[0])
            self.assertIsNone(a['events'][0]['cash_effect']);self.assertEqual(a['reported_runway_months'],10)
