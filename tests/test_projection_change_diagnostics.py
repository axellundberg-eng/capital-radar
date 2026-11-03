"""Synthetic input changes explain staleness without weakening freshness."""
import json
import unittest
from unittest.mock import patch

from tests import test_transaction_value_corrections as fixture_module
from capital_radar.projection_revision import (
    INPUT_FIELDS, ensure_projection_revision_schema,
    read_projection_input_changes, read_projection_revision,
)
from capital_radar.transaction_feed import _build_transaction_items
from capital_radar.transaction_read_model import TransactionReadModel


class ProjectionChangeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.TransactionValueCorrectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db, self.document_id = fixture.db, fixture.document_id
        self.model = TransactionReadModel(self.db.path)
        self.addCleanup(self.model.stop)

    def test_real_source_commit_exposes_only_safe_table_metadata(self):
        self.model.refresh_once()
        before = self.model.feed()['read_model']['revision_diagnostics']
        self.assertEqual(before['unpublished_inputs'], [])
        secret = 'SOURCE_TEXT_AND_PRIVATE_TERMS_MUST_NOT_LEAK'
        with self.db.connect() as c:
            c.execute('UPDATE documents SET title=title||? WHERE id=?', (secret, self.document_id))
        stale = self.model.feed()['read_model']
        self.assertTrue(stale['stale'])
        d = stale['revision_diagnostics']
        self.assertGreater(d['current_source_revision'][1], d['published_source_revision'][1])
        self.assertEqual([x['table'] for x in d['unpublished_inputs']], ['documents'])
        row = d['unpublished_inputs'][0]
        self.assertEqual(row['operation'], 'UPDATE')
        self.assertEqual(row['last_revision'], d['current_source_revision'][1])
        self.assertNotIn(secret, json.dumps(d))
        self.assertNotIn('document_id', json.dumps(d))
        with self.assertLogs('capital_radar.transaction_read_model', level='INFO') as logs:
            self.model.refresh_once()
        logged = '\n'.join(logs.output)
        self.assertIn('read_model_refresh_inputs', logged)
        self.assertIn('documents', logged)
        self.assertNotIn(secret, logged)
        ready = self.model.feed()['read_model']
        self.assertFalse(ready['stale'])
        self.assertEqual(ready['revision_diagnostics']['unpublished_inputs'], [])

    def test_operational_noop_and_rollback_preserve_counts_and_revision(self):
        self.model.refresh_once()
        with self.db.connect() as c:
            before = read_projection_input_changes(c)
            revision = read_projection_revision(c)
            c.execute("UPDATE documents SET title=title,analysis_status='running',updated_at='2099-01-01'")
            c.execute("UPDATE document_sources SET last_seen_at='2099-01-01'")
            self.assertEqual(read_projection_revision(c), revision)
            self.assertEqual(read_projection_input_changes(c), before)
            c.execute('SAVEPOINT cancelled_source_change')
            c.execute("UPDATE documents SET title=title||' changed' WHERE id=?", (self.document_id,))
            self.assertNotEqual(read_projection_input_changes(c), before)
            c.execute('ROLLBACK TO cancelled_source_change')
            c.execute('RELEASE cancelled_source_change')
            self.assertEqual(read_projection_revision(c), revision)
            self.assertEqual(read_projection_input_changes(c), before)
        self.assertFalse(self.model.feed()['read_model']['stale'])

    def test_commit_while_building_remains_stale_with_precise_input(self):
        self.model.refresh_once()
        with self.db.connect() as c:
            c.execute("UPDATE documents SET title=title||' first' WHERE id=?", (self.document_id,))
        def build_from_older_snapshot(source, **kwargs):
            with self.db.connect() as writer:
                writer.execute("UPDATE documents SET title=title||' racing' WHERE id=?", (self.document_id,))
            return _build_transaction_items(source, **kwargs)
        with patch('capital_radar.transaction_read_model._build_transaction_items', side_effect=build_from_older_snapshot):
            self.model.refresh_once()
        stale = self.model.feed()['read_model']
        self.assertTrue(stale['stale'])
        self.assertEqual([x['table'] for x in stale['revision_diagnostics']['unpublished_inputs']], ['documents'])
        self.model.refresh_once()
        self.assertFalse(self.model.feed()['read_model']['stale'])

    def test_install_is_idempotent_and_recreated_store_discards_old_sequence(self):
        with self.db.connect() as c:
            original = [tuple(x) for x in c.execute('SELECT * FROM documents')]
            revision = read_projection_revision(c)
            counters = read_projection_input_changes(c)
            ensure_projection_revision_schema(c, applied_at='2026-10-28')
            self.assertEqual(read_projection_revision(c), revision)
            self.assertEqual(read_projection_input_changes(c), counters)
            c.execute('DELETE FROM dashboard_projection_revision')
            ensure_projection_revision_schema(c, applied_at='2026-10-28')
            self.assertNotEqual(read_projection_revision(c)[0], revision[0])
            self.assertEqual(read_projection_input_changes(c)['changes'], [])
            self.assertEqual([tuple(x) for x in c.execute('SELECT * FROM documents')], original)

    def test_unknown_or_corrupt_diagnostic_values_never_escape(self):
        with self.db.connect() as c:
            c.execute("INSERT INTO dashboard_projection_input_changes VALUES('PRIVATE_TERMS',1,1,'UPDATE','secret')")
            d = read_projection_input_changes(c)
            self.assertEqual(d['status'], 'unavailable')
            self.assertNotIn('PRIVATE_TERMS', json.dumps(d))
            self.assertNotIn('secret', json.dumps(d))

    def test_insert_delete_counters_share_the_source_transaction(self):
        with self.db.connect() as c:
            before = {r['table']: r for r in read_projection_input_changes(c)['changes']}
            row = c.execute("INSERT INTO issuers(canonical_name,normalized_name,created_at,updated_at) VALUES('Synthetic','synthetic-change-counter','2026-01-01','2026-01-01')").lastrowid
            after_insert = {r['table']: r for r in read_projection_input_changes(c)['changes']}['issuers']
            self.assertEqual(after_insert['operation'], 'INSERT')
            self.assertEqual(after_insert['cumulative_change_count'], before.get('issuers', {}).get('cumulative_change_count', 0)+1)
            c.execute('DELETE FROM issuers WHERE id=?', (row,))
            after_delete = {r['table']: r for r in read_projection_input_changes(c)['changes']}['issuers']
            self.assertEqual(after_delete['operation'], 'DELETE')
            self.assertEqual(after_delete['cumulative_change_count'], after_insert['cumulative_change_count']+1)
            self.assertEqual(after_delete['last_revision'], read_projection_revision(c)[1])
            self.assertLessEqual(len(read_projection_input_changes(c)['changes']), len(INPUT_FIELDS))
