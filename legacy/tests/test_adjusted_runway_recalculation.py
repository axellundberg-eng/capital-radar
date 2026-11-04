"""Source cash semantics and immutable adjusted-runway recalc contracts."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from capital_radar.radar_analysis import SCHEMA, calculate, recalculate_adjusted_runway


class SourceCashSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assessment = {
            "id": 1, "issuer_id": 1, "baseline_cash": 10_000_000,
            "monthly_operating_burn": 1_000_000, "operating_runway_months": 10,
            "operating_cash_flow": -3_000_000, "report_period_end": "2026-06-30",
            "currency": "SEK", "report_document_id": 1,
        }

    def transaction(self, key: str, amount: float, **values: object) -> dict:
        return {
            "id": f"transaction:1:{key}:2", "issuer_id": 1,
            "category": "rights_issue", "status": "completed",
            "currency": "SEK", "amount": amount, "amount_basis": "outcome_gross",
            "published_at": "2026-08-26", "source_document_ids": [2],
            "history": [{"published_at": "2026-08-26", "title": key}],
            **values,
        }

    def cash(self, transactions: list[dict]) -> float:
        return calculate(self.assessment, transactions, {}, as_of="2026-09-23")[
            "pro_forma_cash_adjustment"
        ]

    def test_realfiction_round_amount_beats_subscription_intention(self) -> None:
        tx = self.transaction("Realfiction", 700_000, details={
            "announced_amount": 36_000_000,
            "announced_amount_basis": "announced_gross",
        })
        self.assertEqual(self.cash([tx]), 36_000_000)

    def test_axichem_final_to2a_outcome_beats_announcement(self) -> None:
        tx = self.transaction("aXichem TO2A", 15_000_000, category="warrant_exercise",
                              details={"announced_amount": 15_000_000,
                                       "outcome_amount": 12_533_000,
                                       "outcome_amount_basis": "outcome_gross"})
        self.assertEqual(self.cash([tx]), 12_533_000)

    def test_bioextrax_cash_rounds_and_conditional_options(self) -> None:
        directed = self.transaction("Bioextrax-directed", 6_000_000,
                                    category="directed_issue")
        rights = self.transaction("Bioextrax-rights", 4_000_000)
        options = self.transaction("Bioextrax-options", 10_000_000,
                                   category="warrant_exercise", status="conditional")
        self.assertEqual(self.cash([directed, rights, options]), 10_000_000)

    def test_expres2ion_to13_is_separate_from_conditional_put_option(self) -> None:
        to13 = self.transaction("ExpreS2ion-TO13", 32_400_000,
                                category="warrant_exercise")
        put = self.transaction("ExpreS2ion-put", 50_000_000,
                               category="other_equity",
                               amount_basis="conditional_liquidity")
        self.assertEqual(self.cash([to13, put]), 32_400_000)

    def test_rmd_partially_disbursed_loan_uses_confirmed_cash_only(self) -> None:
        loan = self.transaction("RMD", 10_000_000, category="bridge_loan",
                                amount_basis="loan_principal",
                                confirmed_cash_effect_amount=2_400_000,
                                _source_text="Hittills utbetalt 2,4 MSEK. Lösenkurs 1,75 SEK.")
        self.assertEqual(self.cash([loan]), 2_400_000)
        unknown_draw = {**loan, "confirmed_cash_effect_amount": None}
        self.assertEqual(self.cash([unknown_draw]), 0)

    def test_valuno_registered_conversion_is_zero_new_cash(self) -> None:
        tx = self.transaction("Valuno", 9_000_000,
                              confirmed_cash_effect_amount=0,
                              _source_text="Registrerad konvertering av skuld.")
        self.assertEqual(self.cash([tx]), 0)

    def test_bawat_cash_setoff_and_conditional_second_issue_are_separate(self) -> None:
        cash = self.transaction("Bawat-cash", 2_000_000,
                                category="directed_issue",
                                confirmed_cash_effect_amount=2_000_000)
        setoff = self.transaction("Bawat-setoff", 4_000_000,
                                  confirmed_cash_effect_amount=0,
                                  _source_text="Betalning genom kvittning.")
        future = self.transaction("Bawat-second", 3_000_000,
                                  category="directed_issue", status="conditional",
                                  amount_basis="conditional_tranche")
        self.assertEqual(self.cash([cash, setoff, future]), 2_000_000)

    def test_nexar_update_is_one_round_when_linked_to_august_issue(self) -> None:
        tx = self.transaction("Nexar", 8_000_000,
                              source_document_ids=[2, 3],
                              history=[
                                  {"published_at": "2026-08-26", "title": "Emission"},
                                  {"published_at": "2026-09-10", "title": "Uppdatering"},
                              ])
        self.assertEqual(self.cash([tx, tx]), 8_000_000)


class RecalculationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE issuers (id INTEGER PRIMARY KEY)")
        self.connection.execute("INSERT INTO issuers(id) VALUES(1)")
        self.connection.executescript(SCHEMA)
        self.addCleanup(self.connection.close)
        self.before = {"issuer_id": 1, "reported_runway_months": 6.0,
                       "adjusted_runway_months": 7.0, "events": []}
        self.after = {"issuer_id": 1, "reported_runway_months": 6.0,
                      "adjusted_runway_months": 8.5,
                      "events": [{"transaction_id": "t1", "cash_effect": 2_500_000,
                                  "exclusion_reason": None, "correction_ids": [12]}]}

    def test_audit_is_immutable_idempotent_and_returns_before_after(self) -> None:
        with patch("capital_radar.radar_analysis.current_analysis", return_value=[self.after]):
            first = recalculate_adjusted_runway(
                self.connection, 1, idempotency_key="recalc-001", actor="reviewer",
                reason="Rättat kontantbelopp", correction_ids=[12],
                before_snapshot=self.before,
            )
            repeated = recalculate_adjusted_runway(
                self.connection, 1, idempotency_key="recalc-001", actor="reviewer",
                reason="Rättat kontantbelopp", correction_ids=[12],
            )
        self.assertEqual(first, repeated)
        self.assertEqual(first["before"]["adjusted_runway_months"], 7.0)
        self.assertEqual(first["after"]["adjusted_runway_months"], 8.5)
        self.assertEqual(first["correction_ids"], [12])
        self.assertEqual(len(first["calculation_hash"]), 64)
        self.assertEqual(len(first["included_events"]), 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM adjusted_runway_recalculations"
        ).fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE adjusted_runway_recalculations SET status='failed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM adjusted_runway_recalculations")

    def test_idempotency_key_rejects_different_request(self) -> None:
        with patch("capital_radar.radar_analysis.current_analysis", return_value=[self.after]):
            recalculate_adjusted_runway(self.connection, 1,
                                         idempotency_key="recalc-002", actor="reviewer",
                                         reason="First")
            with self.assertRaisesRegex(ValueError, "Idempotency-Key"):
                recalculate_adjusted_runway(self.connection, 1,
                                             idempotency_key="recalc-002", actor="reviewer",
                                             reason="Different")

    def test_reported_runway_change_rejects_audit(self) -> None:
        wrong = {**self.after, "reported_runway_months": 9.0}
        with patch("capital_radar.radar_analysis.current_analysis", return_value=[wrong]):
            with self.assertRaisesRegex(ValueError, "Reported runway"):
                recalculate_adjusted_runway(
                    self.connection, 1, idempotency_key="recalc-003", actor="reviewer",
                    reason="Rättat belopp", before_snapshot=self.before,
                )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM adjusted_runway_recalculations"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
