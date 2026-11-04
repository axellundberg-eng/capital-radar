"""Legacy generic source associations do not fabricate a legal instrument."""
import json
import unittest

import test_rebuild_projection_binding as fixtures
from capital_radar.transaction_corrections import (
    TransactionCorrectionError, apply_transaction_correction,
    revert_transaction_correction, transaction_correction_detail,
)


class NeutralLegacyAssociationTests(unittest.TestCase):
    setUp=fixtures.RebuildProjectionBindingTests.setUp
    cards=fixtures.RebuildProjectionBindingTests.cards
    correction=fixtures.RebuildProjectionBindingTests.correction
    cash=fixtures.RebuildProjectionBindingTests.cash

    def legacy(self,c,description='Legacy generic association for up to 3 steps',financing_type=None):
        row=dict(c.execute('SELECT * FROM financing_events LIMIT 1').fetchone())
        row.pop('id');row.update(event_fingerprint='legacy-neutral',financing_type=financing_type,
            event_type='share_issue',lifecycle_stage=None,canonical_round_key=None,description=description)
        event=c.execute('INSERT INTO financing_events('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values())).lastrowid
        old=c.execute('SELECT extraction_job_id FROM financing_event_evidence LIMIT 1').fetchone()[0]
        c.execute("""INSERT INTO financing_event_evidence(financing_event_id,document_id,extraction_job_id,
            evidence_label,source_excerpt,created_at) VALUES(?,24353,?,'legacy',
            'The clarification concerns the patent description in the memorandum.','2026-10-12')""",(event,old))
        return event

    def test_explicit_review_preserves_legacy_rows_cash_and_target_identity(self):
        with self.db.connect() as c:
            event=self.legacy(c);self.cash(c)
            before=self.cards(c)
            originals=[tuple(r) for r in c.execute('SELECT * FROM financing_events ORDER BY id')]
            links=[tuple(r) for r in c.execute('SELECT * FROM financing_event_evidence ORDER BY id')]
            correction=self.correction(c);apply_transaction_correction(c,correction,actor='fixture')
            cards=self.cards(c);self.assertEqual(len(cards),1)
            self.assertEqual(cards[0]['id'],fixtures.TARGET)
            self.assertEqual(cards[0]['source_document_ids'],fixtures.DOCS)
            self.assertEqual(cards[0]['effective_transaction_values']['confirmed_cash_effect_amount'],0)
            self.assertEqual(cards[0]['grouping_projection_binding']['legacy_neutral_financing_event_ids'],[event])
            self.assertTrue(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM financing_events ORDER BY id')],originals)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM financing_event_evidence ORDER BY id')],links)
            revert_transaction_correction(c,correction,actor='fixture');self.assertEqual(self.cards(c),before)

    def test_later_legacy_event_change_invalidates_membership_without_stranding_revert(self):
        for field,value in [('financing_type','convertible'),('gross_amount',1000000),('description','New TO2 instrument')]:
            with self.subTest(field=field),self.db.connect() as c:
                c.execute('SAVEPOINT scenario');event=self.legacy(c);self.cash(c)
                correction=self.correction(c);apply_transaction_correction(c,correction,actor='fixture')
                c.execute('UPDATE financing_events SET '+field+'=? WHERE id=?',(value,event))
                self.assertFalse(transaction_correction_detail(c,correction)['current_round_matches_applied'])
                core=next(card for card in self.cards(c) if card['id']==fixtures.TARGET)
                self.assertNotIn(24353,core['source_document_ids'])
                self.assertEqual(core['effective_transaction_values']['confirmed_cash_effect_amount'],0)
                revert_transaction_correction(c,correction,actor='fixture')
                c.execute('ROLLBACK TO SAVEPOINT scenario');c.execute('RELEASE SAVEPOINT scenario')

    def test_new_unclassified_evidence_also_requires_new_review(self):
        with self.db.connect() as c:
            self.cash(c);correction=self.correction(c);apply_transaction_correction(c,correction,actor='fixture')
            self.legacy(c)
            self.assertFalse(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            revert_transaction_correction(c,correction,actor='fixture')

    def test_legacy_parent_type_uses_the_same_pinned_neutral_scope(self):
        with self.db.connect() as c:
            event=self.legacy(c);c.execute("UPDATE financing_events SET event_type='rights_issue' WHERE id=?",(event,))
            self.cash(c);correction=self.correction(c);apply_transaction_correction(c,correction,actor='fixture')
            self.assertTrue(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            self.assertEqual(len(self.cards(c)),1)
            c.execute("UPDATE financing_events SET event_type='warrant_exercise' WHERE id=?",(event,))
            self.assertFalse(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            revert_transaction_correction(c,correction,actor='fixture')

    def test_real_options_and_classified_instruments_are_not_legacy_neutral_rows(self):
        for description,kind in [('Separate TO2 instrument',None),('Convertible financing',None),
                ('Directed share issue to guarantors',None),('New unit issue',None),
                ('Credit facility',None),('Source event','rights_issue')]:
            with self.subTest(description=description,kind=kind),self.db.connect() as c:
                c.execute('SAVEPOINT scenario');self.legacy(c,description,kind)
                correction=self.correction(c)
                with self.assertRaisesRegex(TransactionCorrectionError,'grouping_rebuild_separate_instrument'):
                    apply_transaction_correction(c,correction,actor='fixture')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_projection_bindings').fetchone()[0],0)
                c.execute('ROLLBACK TO SAVEPOINT scenario');c.execute('RELEASE SAVEPOINT scenario')


if __name__=='__main__':unittest.main()
