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
from capital_radar.screening_eligibility import classify_screening_eligibility


class ScreeningEligibilityTests(unittest.TestCase):
    def test_complex_and_foreign_issuers_are_skipped_without_ai(self) -> None:
        real_estate = classify_screening_eligibility(
            issuer_name="Exempel Fastigheter",
            jurisdiction_code="SE",
            sectors=("Real Estate",),
        )
        investment = classify_screening_eligibility(
            issuer_name="Exempel Investment Group",
            jurisdiction_code="SE",
            sectors=(),
        )
        foreign_energy = classify_screening_eligibility(
            issuer_name="Foreign Energy",
            jurisdiction_code="CA",
            sectors=("Energy",),
        )
        foreign_medtech = classify_screening_eligibility(
            issuer_name="Foreign Medtech",
            jurisdiction_code="DK",
            sectors=("Health Care",),
        )
        self.assertFalse(real_estate.eligible)
        self.assertFalse(investment.eligible)
        self.assertFalse(foreign_energy.eligible)
        self.assertTrue(foreign_medtech.eligible)
        self.assertEqual(foreign_medtech.priority, 10)
        self.assertTrue(
            classify_screening_eligibility(
                issuer_name="Exempel Medtech Holding AB",
                jurisdiction_code="SE",
                sectors=("Health Care",),
            ).eligible
        )
        for company_name in (
            "Investor AB",
            "Exempel Securities AB",
            "Nordisk Asset Management AB",
            "Svensk Finans AB",
        ):
            with self.subTest(company_name=company_name):
                self.assertFalse(
                    classify_screening_eligibility(
                        issuer_name=company_name,
                        jurisdiction_code="SE",
                    ).eligible
                )

    def test_manual_override_can_include_complex_issuer(self) -> None:
        result = classify_screening_eligibility(
            issuer_name="Exempel Investment",
            jurisdiction_code="SE",
            sectors=("Financials",),
            override=True,
            override_reason="Bergs vill granska bolaget manuellt",
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.code, "manual_include")
        self.assertEqual(result.priority, 0)


class ScreeningBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(
            Path(self.temporary_directory.name) / "screening-batch.db"
        )
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        fixtures = (
            ("MED-1", "Medtech AB", "MED", "SE", "Health Care"),
            ("TECH-1", "Teknik AB", "TECH", "SE", "Technology"),
            ("REAL-1", "Fastighet AB", "REAL", "SE", "Real Estate"),
            ("ENERGY-1", "Foreign Energy Corp", "FEN", "CA", "Energy"),
        )
        for external_id, name, symbol, country, sector in fixtures:
            self.db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id=external_id,
                    issuer_name=name,
                    display_name=name,
                    symbol=symbol,
                    isin=f"SE000000{len(external_id):04d}",
                    currency="SEK",
                    market="Nasdaq First North Growth Market Sweden",
                    segment="First North GM",
                    jurisdiction_code=country,
                    metadata={"sector": sector},
                ),
                run_id,
            )
        self.db.finish_run(run_id, status="success")
        self.service = ManualActionService(
            self.db,
            SimpleNamespace(action_timeout=175.0),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_batch_skips_irrelevant_and_answers_yes_then_no(self) -> None:
        batch = self.service.start_screening_batch(
            refresh_mfn=False,
            prepared_only=False,
        )
        self.assertTrue(batch["created"])
        self.assertEqual(batch["progress"]["total"], 4)
        self.assertEqual(batch["progress"]["skipped"], 2)
        self.assertEqual(batch["current"]["issuer"], "Medtech AB")

        first_action = int(batch["current"]["action_id"])
        first_claim = self.service.claim_action(first_action)
        self.assertIsNotNone(first_claim)
        first_lease, _ = first_claim
        self.assertTrue(
            self.service._complete_claimed_action(
                first_action,
                first_lease,
                "completed",
                {
                    "assessment": {
                        "adjusted_need_score": 85,
                        "need_timing": "3-6_months",
                        "adjusted_runway_months": 3.4,
                    }
                },
            )
        )
        second_action = self.service.record_screening_action(first_action)
        self.assertIsNotNone(second_action)

        second_claim = self.service.claim_action(int(second_action))
        self.assertIsNotNone(second_claim)
        second_lease, _ = second_claim
        self.assertTrue(
            self.service._complete_claimed_action(
                int(second_action),
                second_lease,
                "completed",
                {
                    "assessment": {
                        "adjusted_need_score": 0,
                        "need_timing":
                            "no_negative_operating_cash_flow_signal",
                        "adjusted_runway_months": None,
                    }
                },
            )
        )
        self.assertIsNone(
            self.service.record_screening_action(int(second_action))
        )
        finished = self.service.screening_batch_status(int(batch["id"]))
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["progress"]["capital_need"], 1)
        self.assertEqual(finished["progress"]["no_capital_need"], 1)
        self.assertEqual(finished["progress"]["skipped"], 2)
        self.assertEqual(finished["progress"]["finished"], 4)

    def test_stop_cancels_pending_work_without_running_ai(self) -> None:
        batch = self.service.start_screening_batch(
            refresh_mfn=False,
            prepared_only=False,
        )
        stopped = self.service.stop_screening_batch(int(batch["id"]))
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["progress"]["running"], 0)
        self.assertEqual(stopped["progress"]["pending"], 0)
        self.assertGreaterEqual(stopped["progress"]["cancelled"], 1)

    def test_filter_steps_are_zero_token_and_batch_api_cap_is_hard(self) -> None:
        preview = self.service.screening_filter_preview("ai_call")
        stages = {item["id"]: item for item in preview["stages"]}
        self.assertEqual(stages["company_type"]["input"], 4)
        self.assertEqual(stages["company_type"]["removed"], 1)
        self.assertEqual(stages["foreign"]["removed"], 1)
        self.assertEqual(stages["report"]["remaining"], 0)
        self.assertEqual(preview["projected_new_ai_calls"], 0)

        batch = self.service.start_screening_batch(
            refresh_mfn=False,
            max_api_calls=1,
            prepared_only=False,
        )
        self.assertTrue(
            self.service._reserve_screening_api_call(int(batch["id"]))
        )
        self.assertFalse(
            self.service._reserve_screening_api_call(int(batch["id"]))
        )
        limited = self.service.screening_batch_status(int(batch["id"]))
        self.assertEqual(limited["max_api_calls"], 1)
        self.assertEqual(limited["api_calls_used"], 1)

    def test_default_batch_only_queues_complete_local_reports(self) -> None:
        batch = self.service.start_screening_batch(refresh_mfn=False)
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["progress"]["queued"], 0)
        self.assertEqual(batch["progress"]["running"], 0)
        self.assertEqual(batch["progress"]["deferred"], 2)
        self.assertEqual(batch["progress"]["skipped"], 2)
        self.assertTrue(batch["prepared_only"])

    def test_override_moves_skipped_issuer_into_current_batch(self) -> None:
        batch = self.service.start_screening_batch(
            refresh_mfn=False,
            prepared_only=False,
        )
        with self.db.connect() as connection:
            real_estate_id = int(
                connection.execute(
                    """
                    SELECT i.id FROM issuers i
                    JOIN securities s ON s.issuer_id=i.id
                    WHERE s.external_id='REAL-1'
                    """
                ).fetchone()[0]
            )
        override = self.service.set_screening_override(
            real_estate_id, eligible=True
        )
        self.assertTrue(override["eligible"])
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT status, priority, decision
                FROM screening_batch_items
                WHERE batch_id=? AND issuer_id=?
                """,
                (batch["id"], real_estate_id),
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["priority"], 0)
        self.assertIsNone(row["decision"])


if __name__ == "__main__":
    unittest.main()
