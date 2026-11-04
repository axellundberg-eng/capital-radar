"""Offline regression coverage for bounded automatic orchestration."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from test_auto_analysis import AutoAnalysisCandidateTests as Fixtures
from capital_radar.analysis_worker import AnalysisWorker
from capital_radar.manual_actions import _runway_comparison


class AutonomousAnalysisTests(unittest.TestCase):
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    _add_issuer = Fixtures._add_issuer
    _add_document = Fixtures._add_document
    _set_summary = Fixtures._set_summary
    _add_action = Fixtures._add_action

    def candidates(self):
        selected = self.service.select_analysis_candidates(10)
        self.assertEqual(selected, [
            row["issuer_id"] for row in self.service.list_analysis_candidates(10)
        ])
        return selected

    def ago(self, minutes):
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def test_insufficient_retry_window_and_minimum_interval(self):
        issuer = self._add_issuer("RETRY", "Retry AB")
        self._set_summary(issuer, status="insufficient_facts", last_success_at=self.ago(11))
        for _ in range(4):
            self._add_action(issuer, status="insufficient_facts", started_at=self.ago(1500))
        self.assertIn(issuer, self.candidates())
        for _ in range(3):
            self._add_action(issuer, status="insufficient_facts", started_at=self.ago(11))
        self.assertNotIn(issuer, self.candidates())
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE manual_action_runs SET started_at=?",
                (self.ago(1500),),
            )
        self._add_document(issuer, "fresh-after-cap", self.ago(1))
        self.assertIn(issuer, self.candidates())
        self.assertEqual(self.service.auto_analysis_status()["candidate_count"], 1)

    def test_recent_attempt_blocks_even_new_document(self):
        issuer = self._add_issuer("RECENT", "Recent AB")
        self._set_summary(issuer, status="insufficient_facts", last_success_at=self.ago(5))
        self._add_document(issuer, "new", self.ago(1))
        self.assertNotIn(issuer, self.candidates())

    def test_arrival_triggers_but_extraction_update_does_not(self):
        issuer = self._add_issuer("ARRIVAL", "Arrival AB")
        self._set_summary(issuer, status="completed", last_success_at=self.ago(30))
        self._add_document(issuer, "old", self.ago(120))
        with self.db.connect() as connection:
            connection.execute("UPDATE documents SET updated_at=?", (self.ago(1),))
        self.assertNotIn(issuer, self.candidates())
        with self.db.connect() as connection:
            connection.execute("UPDATE documents SET created_at=?", (self.ago(1),))
        self.assertIn(issuer, self.candidates())

    def test_timestamp_offsets_compare_as_instants(self):
        issuer = self._add_issuer("TZ", "Timezone AB")
        self._set_summary(issuer, status="completed", last_success_at="2026-01-02T10:00:00+02:00")
        self._add_document(issuer, "tz", "2026-01-02 09:00:00")
        self.assertIn(issuer, self.candidates())

    def test_automation_metadata_dedupe_and_config_gate(self):
        issuer = self._add_issuer("AUTO", "Auto AB")
        action = self.service.enqueue_interpret(issuer, trigger_kind="automation")
        self.assertFalse(self.service.enqueue_interpret(issuer, trigger_kind="automation")["created"])
        with self.db.connect() as connection:
            metadata = connection.execute(
                "SELECT metadata_json FROM action_progress_events WHERE action_id=?",
                (action["id"],),
            ).fetchone()[0]
        self.assertEqual(json.loads(metadata)["trigger_kind"], "automation")
        self.settings.auto_analysis_enabled = False
        with self.assertRaises(ValueError):
            self.service.enqueue_interpret(issuer, trigger_kind="automation")
        self.assertEqual(self.service.enqueue_interpret(issuer)["id"], action["id"])
        with self.assertRaises(ValueError):
            AnalysisWorker(self.service).enqueue(issuer, trigger_kind="automation")

    def test_tick_respects_queue_target(self):
        for index in range(3):
            self._add_issuer(str(index), f"Issuer {index}")
        self.settings.auto_analysis_queue_target = 2
        self.settings.auto_analysis_interval_seconds = 0
        worker = AnalysisWorker(self.service)
        worker._auto_analysis_tick()
        worker._auto_analysis_tick()
        self.assertEqual(self.service.count_active_interpret_actions(), 2)
        self.assertEqual(worker._queue.qsize(), 2)

    def test_status_excludes_active_and_retrying_completed_issuers(self):
        issuer = self._add_issuer("STATUS", "Status AB")
        self._set_summary(issuer, status="completed", last_success_at=self.ago(30))
        self.assertEqual(self.service.auto_analysis_status()["up_to_date"], 1)
        self._add_action(issuer, status="insufficient_facts", started_at=self.ago(20))
        status = self.service.auto_analysis_status()
        self.assertEqual(status["up_to_date"], 0)
        self.assertEqual(status["candidates_retry"], 1)
        self.service.enqueue_interpret(issuer)
        status = self.service.auto_analysis_status()
        self.assertEqual(status["up_to_date"], 0)
        self.assertEqual(status["candidate_count"], 0)

    def test_completion_comparison_and_stale_lease(self):
        issuer = self._add_issuer("EVENT", "Event AB")
        action = self.service.enqueue_interpret(issuer, trigger_kind="automation")
        claimed = self.service.claim_action(action["id"])
        with patch.object(self.service, "_interpret_core", return_value=(
            {"prepared": 0, "assessment": None}, "insufficient_facts"
        )):
            self.service.run_claimed_action(action["id"], *claimed)
        with self.db.connect() as connection:
            event = connection.execute(
                "SELECT metadata_json FROM action_progress_events WHERE action_id=? ORDER BY id DESC",
                (action["id"],),
            ).fetchone()[0]
        metadata = json.loads(event)
        self.assertEqual(metadata["outcome"], "awaiting_sources")
        self.assertIsNone(metadata["runway_comparison"]["within_tolerance"])
        self.assertFalse(self.service._complete_claimed_action(
            action["id"], claimed[0], "completed", {}
        ))

    def test_runway_tolerance_and_missing_values(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(_runway_comparison(value, 0)["delta_months"])
            self.assertIsNone(_runway_comparison(0, value)["after_months"])
        for after in (9, 11):
            self.assertTrue(_runway_comparison(10, after)["within_tolerance"])
        self.assertFalse(_runway_comparison(10, 11.01)["within_tolerance"])
        self.assertIsNone(_runway_comparison(None, 0)["delta_months"])
        self.assertEqual(_runway_comparison(0, 0)["delta_months"], 0)
        self.assertEqual(_runway_comparison(10, None)["outcome"], "awaiting_sources")

    def test_worker_accepts_integer_prepared(self):
        actions = Mock(spec=["claim_action", "run_claimed_action", "action_status", "record_screening_action"])
        actions.claim_action.return_value = ("lease", "deadline")
        actions.action_status.return_value = {
            "issuer_id": 1, "status": "completed", "phase": "completed",
            "result": {"prepared": 0},
        }
        actions.record_screening_action.return_value = None
        worker = AnalysisWorker(actions)
        # Kön håller (prioritet, action_id); (99, None) är stoppsignalen.
        worker._queue.put((1, 1))
        worker._queue.put((99, None))
        worker._run()
        actions.record_screening_action.assert_called_once_with(1)


del Fixtures
