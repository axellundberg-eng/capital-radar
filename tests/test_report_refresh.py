from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.db import Database
from capital_radar.models import SecurityCandidate
from capital_radar.report_refresh import ReportRefreshService, ReportRefreshWorker
from capital_radar.sources.mfn import MfnIssuerPageEmptyError


class ReportRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(
            Path(self.temporary_directory.name) / "report-refresh.db"
        )
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        fixtures = (
            ("MED", "Medtech AB", "SE", "Health Care"),
            ("TECH", "Teknik AB", "SE", "Technology"),
            ("REAL", "Fastighet AB", "SE", "Real Estate"),
            ("FOREIGN", "Foreign Energy", "CA", "Energy"),
        )
        for index, (external_id, name, country, sector) in enumerate(
            fixtures, start=1
        ):
            self.db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id=external_id,
                    issuer_name=name,
                    display_name=name,
                    symbol=external_id,
                    isin=f"SE000000{index:04d}",
                    currency="SEK",
                    market="Nasdaq First North Growth Market Sweden",
                    segment="First North GM",
                    jurisdiction_code=country,
                    metadata={"sector": sector},
                ),
                run_id,
            )
        self.db.finish_run(run_id, status="success")
        self.settings = SimpleNamespace(
            action_timeout=175.0,
            http_timeout=1.0,
            min_request_interval=0.0,
            user_agent="test",
            document_allowed_hosts=("storage.mfn.se",),
            document_max_bytes=1_000_000,
            documents_path=Path(self.temporary_directory.name) / "documents",
            mfn_parallel_channels=2,
        )
        self.service = ReportRefreshService(self.db, self.settings)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_all_active_issuers_are_queued_and_active_run_is_reused(
        self,
    ) -> None:
        first = self.service.start_or_resume(trigger_kind="scheduled")
        second = self.service.start_or_resume(trigger_kind="manual")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["progress"]["total"], 4)
        self.assertEqual(first["collection_scope"], "all_active_issuers")

    def test_slice_completes_without_kimi_and_records_each_outcome(self) -> None:
        run = self.service.start_or_resume(trigger_kind="scheduled")
        outcomes = (
            {
                "status": "ready",
                "documents_seen": 1,
                "documents_prepared": 1,
                "reason": "ready",
            },
            {
                "status": "no_report",
                "documents_seen": 2,
                "documents_prepared": 0,
                "reason": "missing",
            },
            {
                "status": "no_report",
                "documents_seen": 1,
                "documents_prepared": 0,
                "reason": "missing",
            },
            {
                "status": "no_report",
                "documents_seen": 1,
                "documents_prepared": 0,
                "reason": "missing",
            },
        )
        with (
            patch.object(
                self.service,
                "_refresh_global_mfn",
                return_value={"seen": 2, "queued": 1},
            ),
            patch.object(
                self.service,
                "_process_issuer",
                side_effect=outcomes,
            ),
        ):
            result = self.service.run_slice(
                int(run["id"]),
                max_seconds=30,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress"]["ready"], 1)
        self.assertEqual(result["progress"]["no_report"], 3)
        self.assertEqual(result["progress"]["finished"], 4)
        self.assertEqual(result["progress"]["news_seen"], 5)
        self.assertEqual(
            result["global_discovery_status"],
            "completed",
        )

    def test_dashboard_resume_does_not_restart_a_paused_refresh(self) -> None:
        run = self.service.start_or_resume(trigger_kind="scheduled")
        run_id = int(run["id"])
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE report_refresh_runs SET status='paused' WHERE id=?",
                (run_id,),
            )
        worker = ReportRefreshWorker(self.service)
        with patch.object(worker, "_launch") as launch:
            worker.resume()
            launch.assert_not_called()
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE report_refresh_runs SET status='queued' WHERE id=?",
                    (run_id,),
                )
            worker.resume()
            launch.assert_called_once_with(run_id)

    def test_worker_loops_slices_until_completed(self) -> None:
        run = self.service.start_or_resume(trigger_kind="manual")
        run_id = int(run["id"])
        worker = ReportRefreshWorker(self.service, max_seconds=30)
        statuses = iter(["paused", "paused", "completed"])
        calls = []

        def fake_slice(rid, *, max_seconds):  # type: ignore[no-untyped-def]
            calls.append(rid)
            status = next(statuses)
            return {
                "status": status,
                "progress": {"finished": 4, "total": 4},
            }

        with patch.object(
            self.service, "run_slice", side_effect=fake_slice
        ):
            worker._run(run_id)
        self.assertEqual(calls, [run_id, run_id, run_id])

    def test_slice_uses_multiple_bounded_mfn_channels(self) -> None:
        run = self.service.start_or_resume(trigger_kind="manual")
        active = 0
        max_active = 0
        lock = threading.Lock()

        def process(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return {
                    "status": "no_report",
                    "documents_seen": 1,
                    "documents_prepared": 0,
                    "reason": "fixture",
                }
            finally:
                with lock:
                    active -= 1

        with (
            patch.object(
                self.service,
                "_refresh_global_mfn",
                return_value={"seen": 2, "queued": 0},
            ),
            patch.object(self.service, "_process_issuer", side_effect=process),
        ):
            result = self.service.run_slice(int(run["id"]), max_seconds=30)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["parallel_channels"], 2)
        self.assertGreaterEqual(max_active, 2)

    def test_legacy_filtered_queue_is_preserved_and_superseded(self) -> None:
        legacy = self.service.start_or_resume(trigger_kind="manual")
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE report_refresh_runs
                SET status='paused', collection_scope='screening_relevant'
                WHERE id=?
                """,
                (int(legacy["id"]),),
            )
        current = self.service.start_or_resume(trigger_kind="manual")
        self.assertNotEqual(current["id"], legacy["id"])
        self.assertEqual(current["collection_scope"], "all_active_issuers")
        self.assertEqual(current["progress"]["total"], 4)
        with self.db.connect() as connection:
            old_status = connection.execute(
                "SELECT status FROM report_refresh_runs WHERE id=?",
                (int(legacy["id"]),),
            ).fetchone()[0]
        self.assertEqual(old_status, "superseded")

    def test_empty_mfn_author_page_is_no_report_not_system_failure(self) -> None:
        with self.db.connect() as connection:
            issuer_id = int(
                connection.execute(
                    """
                    SELECT issuer_id FROM securities
                    WHERE external_id='MED'
                    """
                ).fetchone()[0]
            )
        with (
            patch(
                "capital_radar.report_refresh.prepare_documents",
                return_value={"seen": 0, "prepared": 0},
            ),
            patch.object(
                self.service.store,
                "latest_report_readiness",
                return_value=None,
            ),
            patch.object(
                self.service,
                "_checked_recently",
                return_value=False,
            ),
            patch.object(
                self.service.actions,
                "refresh_mfn",
                side_effect=MfnIssuerPageEmptyError("empty"),
            ),
        ):
            result = self.service._process_issuer(
                issuer_id,
                force_backfill=False,
            )
        self.assertEqual(result["status"], "no_report")
        self.assertIn("Ingen verifierad MFN", result["reason"])


if __name__ == "__main__":
    unittest.main()
