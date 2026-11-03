from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.db import Database
from capital_radar.manual_actions import ManualActionService
from capital_radar.models import SecurityCandidate


def _make_settings(**overrides) -> SimpleNamespace:
    base = {
        "action_timeout": 175.0,
        "http_timeout": 1.0,
        "min_request_interval": 0.0,
        "user_agent": "test",
        "auto_analysis_enabled": True,
        "auto_analysis_interval_seconds": 60,
        "auto_analysis_batch_size": 5,
        "auto_analysis_max_attempts": 3,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class AutoAnalysisCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "auto.db")
        self.db.initialize()
        self.settings = _make_settings()
        self.service = ManualActionService(self.db, self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_issuer(self, external_id: str, name: str) -> int:
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id=external_id,
                issuer_name=name,
                display_name=name,
                symbol=external_id,
                isin=f"SE{external_id:0>10}",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        with self.db.connect() as connection:
            return int(
                connection.execute(
                    "SELECT issuer_id FROM securities WHERE external_id=?",
                    (external_id,),
                ).fetchone()[0]
            )

    def _add_document(
        self,
        issuer_id: int,
        event_key: str,
        published_at: str,
        *,
        cash_relevance_score: int = 50,
        profile_category: str = "report",
        analysis_status: str = "content_ready",
    ) -> None:
        # Default: ett dokument med kassasignal (rapport), sa att det
        # bidrar till latest_doc-filtret i select_analysis_candidates.
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    issuer_id, event_key, canonical_url, title, published_at,
                    relevance_reason, cash_relevance_score, profile_category,
                    analysis_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'test', ?, ?, ?, '2026-01-01', '2026-01-01')
                """,
                (
                    issuer_id,
                    event_key,
                    f"https://mfn.se/{event_key}",
                    f"Rapport {event_key}",
                    published_at,
                    cash_relevance_score,
                    profile_category,
                    analysis_status,
                ),
            )

    def _set_summary(
        self,
        issuer_id: int,
        *,
        status: str,
        last_success_at: str | None = None,
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO issuer_analysis_summary (
                    issuer_id, analysis_status, last_success_at,
                    last_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, '2026-01-01')
                """,
                (issuer_id, status, last_success_at, last_success_at),
            )

    def _add_action(
        self,
        issuer_id: int,
        *,
        status: str,
        started_at: str = "2026-01-01T00:00:00+00:00",
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_action_runs (
                    issuer_id, action_type, status, phase, started_at
                ) VALUES (?, 'interpret_capital_need', ?, 'queued', ?)
                """,
                (issuer_id, status, started_at),
            )

    def test_prioriterar_nya_rapporter_fore_aldrig_analyserade(self) -> None:
        # Issuer A: analyserad men har nytt dokument efter analysen.
        issuer_a = self._add_issuer("AAA", "Alfa AB")
        self._set_summary(
            issuer_a, status="completed", last_success_at="2026-06-01T00:00:00"
        )
        self._add_document(issuer_a, "a-rapport", "2026-08-01T10:00:00")
        # Issuer B: aldrig analyserad, inget dokument.
        issuer_b = self._add_issuer("BBB", "Beta AB")

        candidates = self.service.select_analysis_candidates(10)
        self.assertIn(issuer_a, candidates)
        self.assertIn(issuer_b, candidates)
        self.assertLess(
            candidates.index(issuer_a),
            candidates.index(issuer_b),
            "Bolag med ny rapport ska komma före aldrig-analyserade",
        )

    def test_list_analysis_candidates_ger_reason_och_prioritet(self) -> None:
        issuer_a = self._add_issuer("AAA", "Alfa AB")
        self._set_summary(
            issuer_a, status="completed", last_success_at="2026-06-01T00:00:00"
        )
        self._add_document(issuer_a, "a-rapport", "2026-08-01T10:00:00")
        issuer_b = self._add_issuer("BBB", "Beta AB")

        candidates = self.service.list_analysis_candidates(10)
        by_id = {c["issuer_id"]: c for c in candidates}

        self.assertIn(issuer_a, by_id)
        self.assertIn(issuer_b, by_id)
        self.assertEqual(by_id[issuer_a]["reason"], "new_report")
        self.assertEqual(by_id[issuer_a]["priority"], 0)
        self.assertEqual(by_id[issuer_b]["reason"], "never_analyzed")
        # Prio 1 är reserverad för runway_rule_upgrade.
        self.assertEqual(by_id[issuer_b]["priority"], 2)
        self.assertEqual(by_id[issuer_a]["canonical_name"], "Alfa AB")
        # new_report (prio 0) ska sorteras före never_analyzed (prio 2)
        ids = [c["issuer_id"] for c in candidates]
        self.assertLess(ids.index(issuer_a), ids.index(issuer_b))

    def test_exkluderar_bolag_med_aktiv_action(self) -> None:
        issuer = self._add_issuer("CCC", "Celsius AB")
        self._add_document(issuer, "c-rapport", "2026-08-01T10:00:00")
        self._add_action(issuer, status="queued")

        candidates = self.service.select_analysis_candidates(10)
        self.assertNotIn(issuer, candidates)

    def test_exkluderar_bolag_med_running_action(self) -> None:
        issuer = self._add_issuer("DDD", "Delta AB")
        self._add_document(issuer, "d-rapport", "2026-08-01T10:00:00")
        self._add_action(issuer, status="running")

        candidates = self.service.select_analysis_candidates(10)
        self.assertNotIn(issuer, candidates)

    def test_exkluderar_bolag_som_misslyckats_for_manga_ganger(self) -> None:
        issuer = self._add_issuer("EEE", "Epsilon AB")
        self._add_document(issuer, "e-rapport", "2026-08-01T10:00:00")
        # Tre misslyckade inom de senaste 24h (started_at = nu).
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for _ in range(3):
            self._add_action(issuer, status="failed", started_at=now)

        candidates = self.service.select_analysis_candidates(
            10, max_attempts=3
        )
        self.assertNotIn(issuer, candidates)

    def test_tillater_bolag_med_gamla_misslyckanden(self) -> None:
        issuer = self._add_issuer("FFF", "Fi AB")
        self._add_document(issuer, "f-rapport", "2026-08-01T10:00:00")
        # Tre misslyckade men långt tillbaka i tiden (utanför cooldown).
        for _ in range(3):
            self._add_action(
                issuer, status="failed", started_at="2020-01-01T00:00:00+00:00"
            )

        candidates = self.service.select_analysis_candidates(
            10, max_attempts=3
        )
        self.assertIn(issuer, candidates)

    def test_exkluderar_fardiganalyserade_utan_nya_rapporter(self) -> None:
        issuer = self._add_issuer("GGG", "Gamma AB")
        self._set_summary(
            issuer, status="completed", last_success_at="2026-08-01T00:00:00"
        )
        # Dokumentet är äldre än senaste lyckade analys.
        self._add_document(issuer, "g-rapport", "2026-06-01T10:00:00")

        candidates = self.service.select_analysis_candidates(10)
        self.assertNotIn(issuer, candidates)

    def test_insufficient_facts_far_begransat_aterforsok_utan_ny_rapport(self) -> None:
        # Saknade fakta får återförsök; tidsfönster och minimiintervall
        # testas separat i test_autonomous_analysis.
        issuer = self._add_issuer("III", "Iota AB")
        self._set_summary(issuer, status="insufficient_facts")

        candidates = self.service.select_analysis_candidates(10)
        self.assertIn(issuer, candidates)

    def test_insufficient_facts_om_analyseras_vid_ny_rapport(self) -> None:
        # Men om ett nytt dokument publicerats efter försöket ska bolaget
        # analyseras igen.
        issuer = self._add_issuer("JJJ", "Jota AB")
        self._set_summary(issuer, status="insufficient_facts")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE issuer_analysis_summary "
                "SET last_attempt_at='2026-06-01T00:00:00' WHERE issuer_id=?",
                (issuer,),
            )
        self._add_document(issuer, "j-ny", "2026-08-01T10:00:00")

        candidates = self.service.select_analysis_candidates(10)
        self.assertIn(issuer, candidates)

    def test_metadata_only_relevance_noll_triggar_inte_kandidat(self) -> None:
        # Ett rent metadata_only-dokument med cash_relevance_score 0
        # (t.ex. VD-avgang, pressmeddelande utan kassasignal) ska INTE
        # gora bolaget till kandidat igen — det ar rotorsaken till
        # evighetsloopen (IRLAB/Sleep Cycle).
        issuer = self._add_issuer("MMM", "Metadata AB")
        self._set_summary(
            issuer, status="completed", last_success_at="2026-06-01T00:00:00"
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE issuer_analysis_summary "
                "SET last_attempt_at='2026-06-01T00:00:00' WHERE issuer_id=?",
                (issuer,),
            )
        # Nytt dokument EFTER analysen, men utan kassasignal.
        self._add_document(
            issuer,
            "m-vd-avgang",
            "2026-08-01T10:00:00",
            cash_relevance_score=0,
            profile_category="other",
            analysis_status="metadata_only",
        )

        candidates = self.service.select_analysis_candidates(10)
        self.assertNotIn(issuer, candidates)

    def test_rapport_dokument_triggar_kandidat(self) -> None:
        # Motkontroll: ett dokument med kassasignal (rapport) efter
        # analysen ska fortfarande gora bolaget till kandidat.
        issuer = self._add_issuer("NNN", "Rapport AB")
        self._set_summary(
            issuer, status="completed", last_success_at="2026-06-01T00:00:00"
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE issuer_analysis_summary "
                "SET last_attempt_at='2026-06-01T00:00:00' WHERE issuer_id=?",
                (issuer,),
            )
        self._add_document(issuer, "n-rapport", "2026-08-01T10:00:00")

        candidates = self.service.select_analysis_candidates(10)
        self.assertIn(issuer, candidates)

    def test_batch_size_respekteras(self) -> None:
        for index in range(8):
            issuer = self._add_issuer(f"H{index:02d}", f"Bolag {index} AB")
            self._add_document(
                issuer, f"h-{index}", "2026-08-01T10:00:00"
            )

        candidates = self.service.select_analysis_candidates(3)
        self.assertEqual(len(candidates), 3)


class AutoAnalysisStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "status.db")
        self.db.initialize()
        self.settings = _make_settings()
        self.service = ManualActionService(self.db, self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_issuer(self, external_id: str, name: str) -> int:
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id=external_id,
                issuer_name=name,
                display_name=name,
                symbol=external_id,
                isin=f"SE{external_id:0>10}",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        with self.db.connect() as connection:
            return int(
                connection.execute(
                    "SELECT issuer_id FROM securities WHERE external_id=?",
                    (external_id,),
                ).fetchone()[0]
            )

    def _add_document(
        self,
        issuer_id: int,
        event_key: str,
        published_at: str,
        *,
        cash_relevance_score: int = 50,
        profile_category: str = "report",
        analysis_status: str = "content_ready",
    ) -> None:
        # Default: ett dokument med kassasignal (rapport), sa att det
        # bidrar till latest_doc-filtret i select_analysis_candidates.
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    issuer_id, event_key, canonical_url, title, published_at,
                    relevance_reason, cash_relevance_score, profile_category,
                    analysis_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'test', ?, ?, ?, '2026-01-01', '2026-01-01')
                """,
                (
                    issuer_id,
                    event_key,
                    f"https://mfn.se/{event_key}",
                    f"Rapport {event_key}",
                    published_at,
                    cash_relevance_score,
                    profile_category,
                    analysis_status,
                ),
            )

    def _set_summary(
        self,
        issuer_id: int,
        *,
        status: str,
        last_success_at: str | None = None,
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO issuer_analysis_summary (
                    issuer_id, analysis_status, last_success_at,
                    last_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, '2026-01-01')
                """,
                (issuer_id, status, last_success_at, last_success_at),
            )

    def _add_action(
        self,
        issuer_id: int,
        *,
        status: str,
        started_at: str = "2026-01-01T00:00:00+00:00",
    ) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_action_runs (
                    issuer_id, action_type, status, phase, started_at
                ) VALUES (?, 'interpret_capital_need', ?, 'queued', ?)
                """,
                (issuer_id, status, started_at),
            )

    def test_status_raknar_kategorier_korrekt(self) -> None:
        # A: completed + ny rapport efter analysen -> kandidat (nya rapporter)
        issuer_a = self._add_issuer("SA", "Status Alfa AB")
        self._set_summary(
            issuer_a, status="completed", last_success_at="2026-06-01T00:00:00"
        )
        self._add_document(issuer_a, "sa-1", "2026-08-01T10:00:00")
        # B: aldrig analyserad -> kandidat (aldrig analyserade)
        self._add_issuer("SB", "Status Beta AB")
        # C: up-to-date (completed, dokumentet äldre än analysen)
        issuer_c = self._add_issuer("SC", "Status Cema AB")
        self._set_summary(
            issuer_c, status="completed", last_success_at="2026-08-01T00:00:00"
        )
        self._add_document(issuer_c, "sc-1", "2026-06-01T10:00:00")
        # D: blockerad av aktiv action
        issuer_d = self._add_issuer("SD", "Status Dura AB")
        self._add_document(issuer_d, "sd-1", "2026-08-01T10:00:00")
        self._add_action(issuer_d, status="running")

        status = self.service.auto_analysis_status(max_attempts=3)

        self.assertEqual(status["candidates_new_reports"], 1)
        self.assertEqual(status["candidates_never_analyzed"], 1)
        self.assertEqual(status["blocked_active_action"], 1)
        self.assertEqual(status["blocked_retry_limit"], 0)
        self.assertEqual(status["up_to_date"], 1)
        self.assertIn("recent_actions", status)
        # Minst den aktiva actionen för D ska synas.
        issuer_ids = {row["issuer_id"] for row in status["recent_actions"]}
        self.assertIn(issuer_d, issuer_ids)

    def test_status_raknar_retry_blockerade(self) -> None:
        from datetime import datetime, timezone

        issuer = self._add_issuer("SR", "Status Retry AB")
        self._add_document(issuer, "sr-1", "2026-08-01T10:00:00")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for _ in range(3):
            self._add_action(issuer, status="failed", started_at=now)

        status = self.service.auto_analysis_status(max_attempts=3)
        self.assertEqual(status["blocked_retry_limit"], 1)
        self.assertEqual(status["candidates_never_analyzed"], 0)


class AutoAnalysisSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "sched.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_worker(self, **settings_overrides):
        from capital_radar.analysis_worker import AnalysisWorker

        settings = _make_settings(**settings_overrides)
        service = ManualActionService(self.db, settings)
        return AnalysisWorker(service, poll_interval=0.1), service

    def test_disabled_flagga_stoppar_tick(self) -> None:
        worker, _ = self._make_worker(auto_analysis_enabled=False)
        # Ska inte kasta och ska inte köa något.
        worker._auto_analysis_tick()
        pending = worker.actions.pending_action_ids()
        self.assertEqual(pending, [])

    def test_tick_koar_kandidater(self) -> None:
        worker, service = self._make_worker(
            auto_analysis_interval_seconds=0,
        )
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="SCH",
                issuer_name="Scheduler AB",
                display_name="Scheduler",
                symbol="SCH",
                isin="SE000000SCH0",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        with self.db.connect() as connection:
            issuer_id = int(
                connection.execute(
                    "SELECT issuer_id FROM securities WHERE external_id='SCH'"
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO documents (
                    issuer_id, event_key, canonical_url, title, published_at,
                    relevance_reason, created_at, updated_at
                ) VALUES (?, 'sch-1', 'https://mfn.se/sch-1', 'Rapport',
                          '2026-08-01T10:00:00', 'test', '2026-01-01',
                          '2026-01-01')
                """,
                (issuer_id,),
            )

        worker._auto_analysis_tick()
        pending = service.pending_action_ids()
        self.assertEqual(len(pending), 1)

    def test_tick_throttlas_av_intervall(self) -> None:
        worker, _ = self._make_worker(
            auto_analysis_interval_seconds=3600,
        )
        import time

        worker._last_auto_analysis_at = time.monotonic()
        worker._auto_analysis_tick()
        # Ingen kandidat ska ha hunnit köas (throttlad).
        pending = worker.actions.pending_action_ids()
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
