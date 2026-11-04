"""Owned diagnostics expose current guards without source text or mutations."""
import json
import unittest
from unittest.mock import patch

import test_rebuild_projection_binding as fixtures
from capital_radar.transaction_corrections import (
    TransactionCorrectionError,
    rebuild_projection_guard_diagnostics, safe_grouping_error_code,
)


class GroupingGuardDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.RebuildProjectionBindingTests.setUp
    cards = fixtures.RebuildProjectionBindingTests.cards
    correction = fixtures.RebuildProjectionBindingTests.correction

    def guard(self, connection):
        correction = self.correction(connection)
        row = connection.execute('SELECT * FROM financing_transaction_corrections WHERE id=?', (correction,)).fetchone()
        before = list(connection.iterdump())
        result = rebuild_projection_guard_diagnostics(connection, row,
            json.loads(row['resulting_rounds_json']), self.cards(connection), fixtures.DOCS)
        self.assertEqual(list(connection.iterdump()), before)
        self.assertFalse(result['complete_apply_readiness_verified'])
        self.assertTrue(result['read_only'])
        return result

    def test_current_guard_pass_is_read_only_and_not_complete_apply_readiness(self):
        with self.db.connect() as connection:
            result = self.guard(connection)
            self.assertTrue(result['current_rebuild_projection_guard_passed'])
            self.assertEqual(result['current_validation_code'], 'projection_guard_passed')
            self.assertEqual(result['neutral_candidates'][0]['financing_event_count'], 0)

    def test_shared_parent_event_is_visible_without_misclassifying_the_current_guard(self):
        with self.db.connect() as connection:
            row = connection.execute('SELECT * FROM financing_event_evidence LIMIT 1').fetchone()
            connection.execute("""INSERT INTO financing_event_evidence(financing_event_id,document_id,
                extraction_job_id,evidence_label,source_excerpt,created_at)
                VALUES(?,24353,?,'fixture','PRIVATE_SENTINEL_DO_NOT_EXPOSE','2026-10-25')""",
                (row['financing_event_id'],row['extraction_job_id']))
            result = self.guard(connection)
            candidate = result['neutral_candidates'][0]
            self.assertFalse(result['current_rebuild_projection_guard_passed'])
            self.assertEqual(result['current_validation_code'], 'grouping_rebuild_separate_instrument')
            self.assertEqual(candidate['financing_event_ids'], [row['financing_event_id']])
            self.assertEqual(candidate['events_shared_with_target_sources_count'], 1)
            self.assertEqual(candidate['event_category_matches_target_count'], 1)
            self.assertEqual(candidate['event_issuer_matches_job_count'], 1)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
            self.assertNotIn('29200000', json.dumps(result))

    def test_unknown_guard_exception_never_escapes_as_a_diagnostic_code(self):
        with self.db.connect() as connection:
            with patch('capital_radar.transaction_corrections._prepare_rebuild_binding',
                       side_effect=TransactionCorrectionError('SECRET_PRIVATE_EXCEPTION')):
                result = self.guard(connection)
            self.assertIsNone(result['current_rebuild_projection_guard_passed'])
            self.assertEqual(result['current_validation_code'], 'projection_guard_unclassified')
            self.assertNotIn('SECRET_PRIVATE', json.dumps(result))

    def test_only_complete_literal_guard_codes_cross_the_boundary(self):
        known = 'grouping_rebuild_separate_instrument'
        self.assertEqual(safe_grouping_error_code(TransactionCorrectionError(known)), known)
        self.assertEqual(safe_grouping_error_code(ValueError(known)), known)
        for value in [known + ' SECRET', 'SECRET', 'apply_failed']:
            self.assertIsNone(safe_grouping_error_code(TransactionCorrectionError(value)))
            self.assertIsNone(safe_grouping_error_code(ValueError(value)))
        self.assertIsNone(safe_grouping_error_code(RuntimeError(known)))



if __name__ == '__main__':unittest.main()
