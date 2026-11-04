"""Audited TO2/TO3 source separation; fixtures use synthetic source rows."""
import unittest
from unittest.mock import patch

import test_transaction_feed as feed_tests
from capital_radar.transaction_feed import _categories, _transaction_status, get_transaction_feed
from capital_radar.transaction_corrections import (
    create_transaction_correction, approve_transaction_correction,
    apply_transaction_correction, transaction_correction_detail,
    revert_transaction_correction, GroupingReadbackMismatch,
)


class ReviewedWarrantCompensationTests(unittest.TestCase):
    setUp = feed_tests.TransactionFeedTests.setUp
    add = feed_tests.TransactionFeedTests.add
    add_event = feed_tests.TransactionFeedTests.add_event

    def test_only_audited_completed_single_series_reference_is_removed(self):
        for series in ['TO2', 'TO3']:
            for title in [
                f'XP Chemistries AB genomför riktad nyemission till garanter i samband med avslutad nyttjandeperiod av teckningsoptioner av serie {series}',
                f'Company resolves on a directed share issue to underwriters in connection with the completed exercise period for warrants of series {series}',
            ]:
                with self.subTest(title=title):
                    self.assertEqual(set(_categories(title)), {'directed_issue', 'warrant_exercise'})
                    self.assertEqual(_categories(title, reviewed=True), ['directed_issue'])
                    self.assertIn(_transaction_status(title, '', 'directed_issue', reviewed=True),
                                  {'announced', 'approved'})
        for title in [
            'Company resolves on a directed share issue to underwriters in connection with the future exercise period for warrants of series TO2',
            'Company resolves on a directed share issue to underwriters in connection with the completed exercise period for warrants of series TO2 and TO3',
            "Company resolves on a directed share issue to a guarantor's adviser in connection with the completed exercise period for warrants of series TO2",
            'Company resolves on a directed issue of warrants to underwriters in connection with the completed exercise period for warrants of series TO2',
            'Company resolves on a directed share issue to underwriters in connection with the completed exercise of warrants increasing shares to 30 million',
            'Company resolves on a directed share issue to underwriters and a new exercise period for warrants TO2',
            'Company resolves on a directed share issue to underwriters in connection with the completed exercise period for warrants TO2 and a new rights issue',
        ]:
            with self.subTest(title=title):
                self.assertIn('warrant_exercise', _categories(title, reviewed=True))

    def test_two_series_and_two_directed_issues_remain_four_rounds_atomically(self):
        groups = []
        for series, day, count in [('TO3', '2025-02-10', 4), ('TO2', '2024-10-14', 5)]:
            compensation, _ = self.add(
                f'XP Chemistries AB genomför riktad nyemission till garanter i samband med avslutad nyttjandeperiod av teckningsoptioner av serie {series}',
                date=day+'T12:00:00Z', issuer='XP Chemistries AB',
                body=(f'Teckningsoptioner av serie {series} har utnyttjats. '
                      'Styrelsen har beslutat om en separat riktad emission till garanterna. '
                      'Teckningskursen är 0,07 SEK per aktie. Garantiteckning och garantiersättning i aktier '
                      'har olika betalningsmekanismer. Beslutet är inte en registrering eller mottagen likvid.'))
            event_id = self.add_event(compensation, round_key='raw-xpc-guarantee-issues', stage='announced',
                                      financing_type='directed_issue', gross_amount=0.07,
                                      announcement_date=day, effective_date=day, stage_effective_at=day,
                                      cash_effect_amount=0, liquidity_effect_kind='conditional_liquidity')
            with self.db.connect() as c:
                c.execute('UPDATE financing_event_evidence SET source_excerpt=? WHERE financing_event_id=?',
                          ('riktad emission till garanterna', event_id))
            main = []
            for i in range(count):
                doc, _ = self.add(
                    f'Utfall av teckningsoptioner av serie {series}' if i == 0
                    else f'Villkor för nyttjandeperiod av teckningsoptioner av serie {series}, källa {i}',
                    date=day+'T08:00:00Z', issuer='XP Chemistries AB',
                    body=(f'Teckningsoptioner {series} avser ett eget instrument. Teckningskursen är 0,07 SEK. '
                          'Bolaget tillförs cirka 3,2 MSEK före uppskattade emissionskostnader. '
                          'Garantiernas senare riktade emission är ett separat beslut.'))
                main.append(doc)
            groups.extend([
                {'canonical_round_key':f'xpc-{series.lower()}-exercise', 'document_ids':main},
                {'canonical_round_key':f'xpc-{series.lower()}-directed', 'document_ids':[compensation]},
            ])
        docs = sorted(d for group in groups for d in group['document_ids'])
        self.assertEqual(len(set(docs)), 11)
        with self.db.connect() as c:
            before = get_transaction_feed(c)['items']
            self.assertEqual(len(before), 3, [(x['category'], x['source_document_ids']) for x in before])
            main = next(item for item in before if item['category']=='warrant_exercise')
            original = {table:[tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY id')]
                        for table in ['documents', 'document_contents', 'financing_events',
                                      'financing_event_evidence', 'runway_assessments']}
            correction = create_transaction_correction(c, {
                'correction_type':'split_transaction', 'transaction_id':main['id'],
                'issuer_id':main['issuer_id'], 'source_transaction_document_ids':main['source_document_ids'],
                'additional_document_ids':sorted(set(docs)-set(main['source_document_ids'])),
                'resulting_rounds':groups, 'reason':'Separate the two warrant series and their directed guarantee issues.',
            }, actor='review-agent')
            approve_transaction_correction(c, correction['id'], actor='review-agent')
            with patch('capital_radar.transaction_feed._underwriter_compensation_title', return_value=None), \
                    patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
                with self.assertRaises(GroupingReadbackMismatch):
                    apply_transaction_correction(c, correction['id'], actor='review-agent')
                recalculate.assert_not_called()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_document_overrides WHERE active=1').fetchone()[0], 0)
            apply_transaction_correction(c, correction['id'], actor='review-agent')
            back = transaction_correction_detail(c, correction['id'])
            self.assertTrue(back['current_round_matches_applied'])
            cards = back['effective_feed_current']
            self.assertEqual(len(cards), 4)
            self.assertCountEqual([sorted(card['source_document_ids']) for card in cards],
                                  [sorted(group['document_ids']) for group in groups])
            self.assertEqual(sorted(d for card in cards for d in card['source_document_ids']), docs)
            self.assertEqual([card['category'] for card in cards].count('directed_issue'), 2)
            self.assertEqual([card['category'] for card in cards].count('warrant_exercise'), 2)
            for table, rows in original.items():
                self.assertEqual([tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY id')], rows)
            revert_transaction_correction(c, correction['id'], actor='review-agent')
            restored = get_transaction_feed(c)['items']
            self.assertCountEqual([(card['id'], card['source_document_ids']) for card in restored],
                                  [(card['id'], card['source_document_ids']) for card in before])


if __name__ == '__main__':
    unittest.main()
