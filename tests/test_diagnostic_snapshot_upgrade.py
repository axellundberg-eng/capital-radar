"""Reuse only the proven metadata-only tracker installation; retain all fences."""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from tests import test_transaction_value_corrections as fixtures
from capital_radar import projection_revision as revision
from capital_radar import transaction_read_model as projection


class DiagnosticSnapshotUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.current_projection_version=projection.PROJECTION_VERSION
        f=fixtures.TransactionValueCorrectionTests();f.setUp();self.addCleanup(f.doCleanups)
        self.db,self.doc=f.db,f.document_id
        self.model=projection.TransactionReadModel(self.db.path);self.addCleanup(self.model.stop)
        # Reproduce the deployed v19 payload/v1 revision. Diagnostic counters
        # did not exist before that historical version's tracker installation.
        with patch.object(revision,'TRACKER_VERSION','dashboard-projection-inputs-v1'):
            with self.db.connect() as c:
                revision.ensure_projection_revision_schema(c,applied_at='2026-10-28')
                c.execute('DELETE FROM dashboard_projection_input_changes')
            with patch.object(projection,'PROJECTION_VERSION',projection.DIAGNOSTIC_PREDECESSOR):
                self.model.refresh_once()
        self.original=self.source_rows()
        self.payloads=self.cache_payloads()
        # Exercise the historical v19->v20 compatibility proof explicitly.
        # The current semantic projection version must never take that shortcut.
        historical=patch.object(projection,'PROJECTION_VERSION',projection.DIAGNOSTIC_SUCCESSOR)
        historical.start();self.addCleanup(historical.stop)

    def test_semantic_projection_version_rebuilds_even_for_diagnostics_only_install(self):
        self.install()
        self.assertNotEqual(self.current_projection_version,projection.DIAGNOSTIC_SUCCESSOR)
        with patch.object(projection,'PROJECTION_VERSION',self.current_projection_version):
            self.assert_rebuild()
            self.assertEqual(self.source_rows(),self.original)
            self.assertEqual(self.model.feed()['read_model']['status'],'ready')

    def test_semantic_projection_version_rebuilds_an_existing_v20_snapshot(self):
        self.install();self.model.refresh_once()
        self.assertEqual(self.upgrade_count(),1)
        with patch.object(projection,'PROJECTION_VERSION',self.current_projection_version), \
                patch.object(projection,'_build_transaction_items',wraps=projection._build_transaction_items) as build:
            self.assertTrue(self.model.refresh_once())
            self.assertEqual(build.call_count,1)
        self.assertEqual(self.upgrade_count(),1)
        self.assertEqual(self.source_rows(),self.original)

    def test_warrant_compensation_version_rebuilds_existing_v21_snapshot(self):
        self.install()
        with patch.object(projection, 'PROJECTION_VERSION',
                          'dashboard-read-model-v21-reviewed-singular-guarantor:' + projection.RADAR_VERSION):
            self.assertTrue(self.model.refresh_once())
        with patch.object(projection, 'PROJECTION_VERSION', self.current_projection_version), \
                patch.object(projection, '_build_transaction_items', wraps=projection._build_transaction_items) as build:
            self.assertTrue(self.model.refresh_once())
            self.assertEqual(build.call_count, 1)
        self.assertEqual(self.upgrade_count(), 0)
        self.assertEqual(self.source_rows(), self.original)

    def source_rows(self):
        with self.db.connect() as c:
            return {t:[tuple(r)for r in c.execute('SELECT * FROM '+t)]
                    for t in ['documents','document_contents','financing_events','runway_assessments']}

    def cache_payloads(self):
        with closing(sqlite3.connect(self.model.path)) as c:
            rows={t:[tuple(r)for r in c.execute('SELECT * FROM '+t)]
                  for t in ['projection_transactions','projection_notifications','projection_analyses']}
            rows['market_health']=[tuple(r)for r in c.execute('SELECT market_health_json FROM projection_meta')]
            return rows

    def install(self):
        with self.db.connect() as c:revision.ensure_projection_revision_schema(c,applied_at='2026-10-28')

    def upgrade_count(self):
        with closing(sqlite3.connect(self.model.path)) as c:
            return c.execute('SELECT COUNT(*) FROM projection_metadata_upgrades').fetchone()[0]

    def assert_rebuild(self):
        with patch.object(projection,'_build_transaction_items',wraps=projection._build_transaction_items) as build:
            self.model.refresh_once();self.assertEqual(build.call_count,1)
        self.assertEqual(self.upgrade_count(),0)

    def change_title(self):
        with self.db.connect() as c:c.execute("UPDATE documents SET title=replace(title,'30 MSEK','40 MSEK') WHERE id=?",(self.doc,))

    def test_exact_metadata_install_reuses_byte_identical_payload_and_records_receipt(self):
        self.install()
        old=self.model.feed();self.assertTrue(old['read_model']['stale']);self.assertTrue(old['items'])
        with patch.object(projection,'_build_transaction_items',side_effect=AssertionError('must not rebuild')):
            self.assertTrue(self.model.refresh_once())
            self.assertFalse(self.model.refresh_once())
        self.assertEqual(self.cache_payloads(),self.payloads)
        self.assertEqual(self.source_rows(),self.original)
        self.assertEqual(self.upgrade_count(),1)
        self.assertEqual(self.model.feed()['read_model']['status'],'ready')
        with closing(sqlite3.connect(self.model.path)) as c:
            self.assertEqual(c.execute('SELECT reason FROM projection_metadata_upgrades').fetchone()[0],'diagnostics_only_tracker_upgrade')

    def test_financial_source_change_before_install_requires_graph(self):
        self.change_title();self.install();self.assert_rebuild()
        self.assertEqual(self.model.feed()['items'][0]['amount'],40000000)

    def test_financial_source_change_after_install_requires_graph(self):
        self.install();self.change_title();self.assert_rebuild()
        self.assertEqual(self.model.feed()['items'][0]['amount'],40000000)

    def test_utc_day_change_requires_graph(self):
        self.install();tomorrow=datetime.now(timezone.utc)+timedelta(days=1)
        with patch.object(projection,'datetime',wraps=datetime) as clock:
            clock.now.return_value=tomorrow;self.assert_rebuild()

    def test_different_store_cannot_reuse_snapshot(self):
        self.install()
        with self.db.connect() as c:c.execute("UPDATE dashboard_projection_revision SET store_id='another-store'")
        self.assert_rebuild()

    def test_unavailable_tracking_does_not_enable_metadata_upgrade(self):
        self.install()
        with self.db.connect() as c:c.execute("INSERT INTO dashboard_projection_input_changes VALUES('invalid',1,1,'UPDATE','private')")
        self.assert_rebuild()
        self.assertEqual(self.model.feed()['read_model']['revision_diagnostics']['input_tracking_status'],'unavailable')

    def test_failed_cache_update_rolls_back_upgrade_and_keeps_previous_payloads(self):
        self.install()
        with closing(sqlite3.connect(self.model.path)) as c:
            c.execute("CREATE TRIGGER reject_upgrade BEFORE UPDATE ON projection_meta BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.model.refresh_once()
        self.assertEqual(self.upgrade_count(),0);self.assertEqual(self.cache_payloads(),self.payloads)
        self.assertTrue(self.model.feed()['read_model']['stale'])
        with closing(sqlite3.connect(self.model.path)) as c:c.execute('DROP TRIGGER reject_upgrade');c.commit()
        self.model.refresh_once();self.assertEqual(self.upgrade_count(),1)

    def test_changed_input_column_signature_requires_graph(self):
        with self.db.connect() as c:c.execute('ALTER TABLE financing_events ADD COLUMN synthetic_input INTEGER')
        self.install();self.assert_rebuild()

    def test_source_commit_after_proof_is_still_stale_and_needs_next_build(self):
        self.install()
        real_uuid=projection.uuid.uuid4;called=False
        def commit_after_proof():
            nonlocal called
            if not called:self.change_title();called=True
            return real_uuid()
        with patch.object(projection.uuid,'uuid4',side_effect=commit_after_proof):
            self.model.refresh_once()
        self.assertTrue(called);self.assertTrue(self.model.feed()['read_model']['stale'])
        self.assertEqual(self.cache_payloads(),self.payloads)
        self.model.refresh_once();self.assertFalse(self.model.feed()['read_model']['stale'])
        self.assertEqual(self.model.feed()['items'][0]['amount'],40000000)
