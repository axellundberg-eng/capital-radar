"""Full archives remain part of the proof even when display contents are capped."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import test_rebuild_projection_binding as fixtures
from capital_radar.evidence_store import archive_document_evidence
from capital_radar.transaction_corrections import (
    TransactionCorrectionError, apply_transaction_correction,
    approve_transaction_correction, revert_transaction_correction,
    transaction_correction_detail,
)


class NeutralArchiveScopeTests(unittest.TestCase):
    setUp=fixtures.RebuildProjectionBindingTests.setUp
    cards=fixtures.RebuildProjectionBindingTests.cards
    correction=fixtures.RebuildProjectionBindingTests.correction
    cash=fixtures.RebuildProjectionBindingTests.cash

    def archive(self,c,text):
        return archive_document_evidence(c,document_id=24353,content_id=None,text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),page_count=None)

    def test_archive_tail_change_and_loss_invalidate_without_changing_display_text(self):
        for change in ['tail','missing']:
            with self.subTest(change=change),self.db.connect() as c:
                c.execute('SAVEPOINT scenario')
                text='Neutral memorandum clarification.'+' '*130000+'First archive tail.'
                self.archive(c,text);self.cash(c)
                display=c.execute('SELECT extracted_text FROM document_contents WHERE document_id=24353').fetchone()[0]
                correction=self.correction(c);apply_transaction_correction(c,correction,actor='fixture')
                self.assertTrue(transaction_correction_detail(c,correction)['current_round_matches_applied'])
                if change=='tail':self.archive(c,text.replace('First archive tail.','Changed archive tail.'))
                else:c.execute('DELETE FROM document_evidence_texts WHERE document_id=24353')
                self.assertEqual(c.execute('SELECT extracted_text FROM document_contents WHERE document_id=24353').fetchone()[0],display)
                self.assertFalse(transaction_correction_detail(c,correction)['current_round_matches_applied'])
                self.assertEqual(next(card for card in self.cards(c) if card['id']==fixtures.TARGET)['effective_transaction_values']['confirmed_cash_effect_amount'],0)
                revert_transaction_correction(c,correction,actor='fixture')
                c.execute('ROLLBACK TO SAVEPOINT scenario');c.execute('RELEASE SAVEPOINT scenario')

    def test_unknown_old_archive_proof_cannot_be_current_verified(self):
        with self.db.connect() as c:
            self.archive(c,'Neutral archive text');correction=self.correction(c)
            apply_transaction_correction(c,correction,actor='fixture')
            stored=json.loads(c.execute('SELECT neutral_source_fingerprints_json FROM financing_transaction_projection_bindings WHERE correction_id=?',(correction,)).fetchone()[0])
            stored['24353'].pop('archive_text_sha256')
            c.execute('UPDATE financing_transaction_projection_bindings SET neutral_source_fingerprints_json=? WHERE correction_id=?',(json.dumps(stored),correction))
            self.assertFalse(transaction_correction_detail(c,correction)['current_round_matches_applied'])
            revert_transaction_correction(c,correction,actor='fixture')


if __name__=='__main__':unittest.main()
