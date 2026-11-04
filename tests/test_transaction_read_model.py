"""Disk projection correctness, refresh isolation and production HTTP boundaries."""
import json
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from tests import test_transaction_value_corrections as value_fixture
from capital_radar.models import DocumentCandidate
from capital_radar.transaction_feed import _build_transaction_items, get_transaction_feed
from capital_radar.transaction_read_model import TransactionReadModel
from capital_radar.projection_revision import (
    INPUT_FIELDS, ProjectionRevisionUnavailable,
    ensure_projection_revision_schema, read_projection_revision,
)
from capital_radar.transaction_value_corrections import (
    create_transaction_value_correction, approve_transaction_value_correction,
    apply_transaction_value_correction, revert_transaction_value_correction,
)


class TransactionReadModelTests(unittest.TestCase):
    def setUp(self):
        fixture = value_fixture.TransactionValueCorrectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db, self.document_id, self.issuer_id, self.source = fixture.db, fixture.document_id, fixture.issuer_id, fixture.source
        self.model = TransactionReadModel(self.db.path, interval_seconds=0.1)
        self.addCleanup(self.model.stop)

    def source_rows(self):
        with self.db.connect() as source:
            return {table: [tuple(row) for row in source.execute("SELECT * FROM " + table)]
                    for table in ("documents", "document_contents", "runway_assessments")}

    def test_pending_restart_and_reads_preserve_volume_sources(self):
        original = self.source_rows()
        pending = self.model.feed()
        self.assertEqual(pending["read_model"]["status"], "pending")
        self.assertIsNone(pending["read_model"]["generation"])
        self.assertTrue(self.model.refresh_once())
        payload = self.model.feed()
        self.assertEqual(payload["read_model"]["status"], "ready")
        self.assertEqual(self.model.path.parent, self.db.path.parent.resolve())
        restarted = TransactionReadModel(self.db.path)
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.feed()["items"], payload["items"])
        self.assertTrue(restarted.feed()["read_model"]["stale"])
        self.assertFalse(restarted.refresh_once())
        self.assertEqual(restarted.analysis()["read_model"]["generation"], payload["read_model"]["generation"])
        self.assertEqual(restarted.health()["read_model"]["generation"], payload["read_model"]["generation"])
        self.assertEqual(self.source_rows(), original)

    def test_correction_apply_revert_and_strict_readback_do_not_use_stale_projection(self):
        self.model.refresh_once()
        old = self.model.feed()["items"][0]
        original = self.source_rows()
        with self.db.connect() as source:
            correction = create_transaction_value_correction(source, {
                "transaction_id": old["id"], "issuer_id": self.issuer_id,
                "field_name": "amount", "expected_previous_value": old["amount"],
                "proposed_value": 36_000_000, "source_document_id": self.document_id,
                "source_excerpt": self.source, "confidence": .97, "rationale": "Verifierad total likvid",
            }, actor="test-agent")
            approve_transaction_value_correction(source, correction["id"], actor="test-agent")
            applied = apply_transaction_value_correction(source, correction["id"], actor="test-agent")
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(get_transaction_feed(source)["items"][0]["amount"], 36_000_000)
        stale = self.model.feed()
        self.assertTrue(stale["read_model"]["stale"])
        self.assertEqual(stale["items"][0]["amount"], old["amount"])
        self.model.refresh_once()
        corrected = self.model.feed()["items"][0]
        self.assertEqual(corrected["amount"], 36_000_000)
        self.assertEqual(corrected["applied_transaction_correction_ids"], [correction["id"]])
        self.assertEqual(self.model.analysis()["items"][0]["reported_runway_months"], 10)
        with self.db.connect() as source:
            revert_transaction_value_correction(source, correction["id"], actor="test-agent")
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        self.model.refresh_once()
        self.assertEqual(self.model.feed()["items"][0]["amount"], old["amount"])
        self.assertEqual(self.source_rows(), original)

    def test_same_length_direct_updates_and_unrelated_writes(self):
        self.model.refresh_once()
        generation = self.model.feed()["read_model"]["generation"]
        with self.db.connect() as source:
            source.execute("UPDATE documents SET title=replace(title,'30 MSEK','40 MSEK') WHERE id=?", (self.document_id,))
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        self.assertTrue(self.model.refresh_once())
        self.assertEqual(self.model.feed()["items"][0]["amount"], 40_000_000)
        generation = self.model.feed()["read_model"]["generation"]
        with self.db.connect() as source:
            source.execute("CREATE TABLE irrelevant_operational_state(id INTEGER)")
            source.execute("INSERT INTO irrelevant_operational_state VALUES(1)")
        self.assertFalse(self.model.feed()["read_model"]["stale"])
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=AssertionError("must not rebuild")):
            self.assertFalse(self.model.refresh_once())
        self.assertEqual(self.model.feed()["read_model"]["generation"], generation)
        self.assertFalse(self.model.feed()["read_model"]["stale"])

    def test_tracker_installation_is_idempotent_and_does_not_touch_originals(self):
        original = self.source_rows()
        with self.db.connect() as source:
            before = read_projection_revision(source)
            ensure_projection_revision_schema(source, applied_at="2026-10-06")
            self.assertEqual(read_projection_revision(source), before)
            triggers = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertTrue({f"trg_dashboard_projection_{table}_{operation}" for table in INPUT_FIELDS
                             for operation in ("insert", "update", "delete")}.issubset(triggers))
        self.assertEqual(self.source_rows(), original)

    def test_noop_operational_updates_and_rollback_do_not_change_revision(self):
        self.model.refresh_once()
        with self.db.connect() as source:
            before = read_projection_revision(source)
            source.execute("UPDATE documents SET title=title,analysis_status='running',updated_at='2099-01-01'")
            source.execute("UPDATE document_sources SET last_seen_at='2099-01-01',metadata_json='{}'")
            source.execute("UPDATE document_contents SET updated_at='2099-01-01',text_sha256='unchanged-body'")
            self.assertEqual(read_projection_revision(source), before)
            source.execute("SAVEPOINT cancelled_correction")
            source.execute("UPDATE documents SET title=title||' changed'")
            self.assertGreater(read_projection_revision(source)[1], before[1])
            source.execute("ROLLBACK TO cancelled_correction")
            source.execute("RELEASE cancelled_correction")
            self.assertEqual(read_projection_revision(source), before)
        self.assertFalse(self.model.feed()["read_model"]["stale"])

    def test_radar_weights_and_assessments_are_relevant_inputs(self):
        # CRM följer inte med till Capital Radar och är ingen indata längre.
        self.model.refresh_once()
        old = self.model.analysis()["items"][0]
        with self.db.connect() as source:
            source.execute("UPDATE runway_assessments SET baseline_cash=baseline_cash+1000000")
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        self.model.refresh_once()
        new = self.model.analysis()["items"][0]
        self.assertEqual(new["reported_runway_months"], old["reported_runway_months"])
        self.assertEqual(new["adjusted_runway_months"], old["adjusted_runway_months"]+1)
        transaction_id = self.model.feed()["items"][0]["id"]
        with self.db.connect() as source:
            source.execute("INSERT INTO radar_financing_weights(issuer_id,transaction_id,weight,actor,reason,created_at) VALUES(?,?,0.5,'test','test','2026-10-06')", (self.issuer_id, transaction_id))
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        self.model.refresh_once()
        self.assertNotEqual(self.model.analysis()["items"][0]["adjusted_runway_months"], new["adjusted_runway_months"])

    def test_missing_tracker_fails_closed_and_repair_invalidates_cached_generation(self):
        self.model.refresh_once()
        generation = self.model.feed()["read_model"]["generation"]
        with self.db.connect() as source:
            source.execute("DELETE FROM dashboard_projection_revision")
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        with self.assertRaises(ProjectionRevisionUnavailable):
            self.model.refresh_once()
        self.assertEqual(self.model.feed()["read_model"]["generation"], generation)
        with self.db.connect() as source:
            ensure_projection_revision_schema(source, applied_at="2026-10-06")
        self.assertTrue(self.model.refresh_once())
        self.assertNotEqual(self.model.feed()["read_model"]["generation"], generation)

    def test_sql_pagination_filters_without_graph_build(self):
        for index in range(14):
            self.db.upsert_document(DocumentCandidate(source_id="mfn", external_id=f"projection-{index}",
                issuer_name=f"Projection Issuer {index}", title=f"Företrädesemission om {index + 1} MSEK",
                canonical_url=f"https://mfn.se/a/projection/{index}", published_at=f"2026-09-{index+1:02}T08:00:00Z", language="sv"), universe_only=False)
        self.model.refresh_once()
        with self.db.connect() as source:
            expected = get_transaction_feed(source, page=2)
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=AssertionError("must not rebuild")):
            page = self.model.feed(page=2)
            self.assertEqual(page["items"], expected["items"])
            self.assertEqual(page["total"], expected["total"])
            self.assertEqual(len(page["items"]), 5)
            self.assertEqual(self.model.feed(q="Realfiction")["total"], 1)
            self.assertEqual(self.model.feed(category="directed_issue")["total"], 0)
            self.assertEqual(self.model.feed(stage="announcement", from_date="2026-09-20")["total"], 1)
        for arguments in ({"from_date": "2026-02-30"}, {"category": "invalid"}, {"page_size": 11}):
            with self.assertRaises(ValueError):
                self.model.feed(**arguments)

    def test_reader_serves_last_snapshot_while_refresh_is_blocked(self):
        self.model.refresh_once()
        entered, release = threading.Event(), threading.Event()
        with self.db.connect() as source:
            source.execute("UPDATE documents SET title=replace(title,'30 MSEK','40 MSEK') WHERE id=?", (self.document_id,))
        def blocked(source, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return _build_transaction_items(source, **kwargs)
        errors = []
        def refresh():
            try:
                self.model.refresh_once()
            except Exception as error:
                errors.append(error)
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=blocked):
            thread = threading.Thread(target=refresh)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                cached = self.model.feed()
                self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(cached["items"][0]["amount"], 30_000_000)
                self.assertTrue(cached["read_model"]["stale"])
                self.assertTrue(cached["read_model"]["refreshing"])
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(self.model.feed()["items"][0]["amount"], 40_000_000)

    def test_commit_during_snapshot_is_marked_stale_and_corrects_next_refresh(self):
        def commit_after_snapshot(source, **kwargs):
            with self.db.connect() as writer:
                writer.execute("UPDATE documents SET title=replace(title,'30 MSEK','40 MSEK') WHERE id=?", (self.document_id,))
            return _build_transaction_items(source, **kwargs)
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=commit_after_snapshot):
            self.model.refresh_once()
        self.assertEqual(self.model.feed()["items"][0]["amount"], 30_000_000)
        self.assertTrue(self.model.feed()["read_model"]["stale"])
        self.model.refresh_once()
        self.assertEqual(self.model.feed()["items"][0]["amount"], 40_000_000)
        self.assertFalse(self.model.feed()["read_model"]["stale"])

    def test_failed_refresh_keeps_last_snapshot_with_explicit_error(self):
        self.model.refresh_once()
        old = self.model.feed()
        with self.db.connect() as source:
            source.execute("UPDATE documents SET title=title||' ' WHERE id=?", (self.document_id,))
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=TimeoutError("private text must not escape")):
            with self.assertRaises(TimeoutError):
                self.model.refresh_once()
        failed = self.model.feed()
        self.assertEqual(failed["items"], old["items"])
        self.assertEqual(failed["read_model"]["status"], "failed")
        self.assertEqual(failed["read_model"]["error_code"], "refresh_timeout")

    def test_background_worker_starts_and_retries_temporary_source_failure(self):
        import sqlite3
        finished = threading.Event()
        real_source = self.model._source
        real_refresh = self.model.refresh_once
        attempts = []
        def flaky_source():
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("temporary source lock")
            return real_source()
        def refresh():
            result = real_refresh()
            finished.set()
            return result
        with patch.object(self.model, "_source", side_effect=flaky_source), patch.object(self.model, "refresh_once", side_effect=refresh):
            self.model.start()
            self.assertTrue(finished.wait(3))
            self.model.stop()
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(self.model.feed()["read_model"]["status"], "ready")

    def test_background_ignores_file_stamp_without_source_revision_change(self):
        import os
        finished = threading.Event()
        real_refresh = self.model.refresh_once
        def refresh():
            result = real_refresh()
            finished.set()
            return result
        with patch.object(self.model, "refresh_once", side_effect=refresh):
            self.model.start()
            self.assertTrue(finished.wait(3))
            generation = self.model.feed()["read_model"]["generation"]
            finished.clear()
            info = self.db.path.stat()
            os.utime(self.db.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            self.assertFalse(self.model.feed()["read_model"]["stale"])
            self.assertFalse(finished.wait(.3))
            self.model.stop()
        self.assertEqual(self.model.feed()["read_model"]["generation"], generation)
        self.assertFalse(self.model.feed()["read_model"]["stale"])

    def test_cold_timeout_and_failed_atomic_publish_can_retry(self):
        with patch("capital_radar.transaction_read_model._build_transaction_items", side_effect=TimeoutError("deadline")):
            with self.assertRaises(TimeoutError):
                self.model.refresh_once()
        cold = self.model.feed()
        self.assertIsNone(cold["read_model"]["generation"])
        self.assertEqual(cold["read_model"]["status"], "failed")
        self.model.refresh_once()
        old = self.model.feed()
        with self.db.connect() as source:
            source.execute("UPDATE documents SET title=replace(title,'30 MSEK','40 MSEK') WHERE id=?", (self.document_id,))
        import sqlite3
        with closing(sqlite3.connect(self.model.path)) as cache:
            cache.execute("CREATE TRIGGER fail_publication BEFORE INSERT ON projection_transactions BEGIN SELECT RAISE(ABORT,'test failure'); END")
            cache.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.model.refresh_once()
        failed = self.model.feed()
        self.assertEqual(failed["items"], old["items"])
        self.assertEqual(failed["read_model"]["generation"], old["read_model"]["generation"])
        with closing(sqlite3.connect(self.model.path)) as cache:
            cache.execute("DROP TRIGGER fail_publication")
            cache.commit()
        self.model.refresh_once()
        self.assertEqual(self.model.feed()["items"][0]["amount"], 40_000_000)
        self.assertEqual(self.model.feed()["read_model"]["status"], "ready")

    def test_large_text_history_constant_revision_and_in_place_text_edit(self):
        # Source text stays on disk; this synthetic 8.4 MB report corpus exercises
        # O(1) revisions without reading historical text to validate the cache.
        body = "Historisk rapporttext utan finansiering. " * 3200
        with self.db.connect() as source:
            for index in range(70):
                source.execute("INSERT INTO document_contents(document_id,source_url,extracted_text,text_sha256,char_count,status,created_at,updated_at) VALUES(?,?,?,?,?,'extracted',?,?)",
                               (self.document_id, f"https://mfn.se/fixture/report-{index}", body, f"fixture-{index}", len(body), "2026-01-01", "2026-01-01"))
            source.execute("UPDATE document_contents SET quality_score=100 WHERE source_url='https://mfn.se/a/realfiction/round'")
        started = time.monotonic()
        with closing(self.model._source()) as source:
            statements = []
            source.set_trace_callback(statements.append)
            self.model._fingerprint(source, lambda: None)
        self.assertEqual(len(statements), 1)
        self.assertIn("FROM dashboard_projection_revision", statements[0])
        self.assertLess(time.monotonic()-started, .5)
        self.model.refresh_once()
        print("projection synthetic source bytes=", len(body.encode()) * 70, "refresh_seconds=", round(time.monotonic()-started, 3))
        generation = self.model.feed()["read_model"]["generation"]
        with self.db.connect() as source:
            source.execute("UPDATE document_contents SET extracted_text=replace(extracted_text,'Historisk','HistorisK') WHERE source_url='https://mfn.se/fixture/report-1'")
        self.assertTrue(self.model.refresh_once())
        self.assertNotEqual(self.model.feed()["read_model"]["generation"], generation)
        self.assertEqual(len(self.model.feed()["items"]), 1)

