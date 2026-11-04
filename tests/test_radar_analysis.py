"""Contract tests for the additive model, independent of screening scores."""
import copy
import json
import sqlite3
import unittest
import tempfile
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from capital_radar.radar_analysis import SCHEMA, calculate, capture_month_end, current_analysis, save_weight
from capital_radar.db import Database
from capital_radar.models import DocumentCandidate


class RadarAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.assessment = dict(issuer_id=1, baseline_cash=60, monthly_operating_burn=10,
            operating_cash_flow=-30, operating_runway_months=6, report_period_end="2026-06-30",
            currency="SEK", id=9, report_document_id=2, adjusted_need_score=88)
        self.tx = dict(id="transaction:1:rights_issue:3", issuer_id=1, category="rights_issue",
            status="announced", currency="SEK", amount=30, amount_basis="announced_gross",
            published_at="2026-07-15", source_document_ids=[3],
            history=[dict(published_at="2026-07-15", title="Nyemission")])

    def calc(self, transactions=None, weights=None):
        return calculate(self.assessment, transactions if transactions is not None else [self.tx],
                         weights or {}, as_of="2026-08-31")

    def test_cash_rolls_forward_and_manual_weights_apply(self):
        # 62 dagar efter periodslut: 60 - 10 * 2,04 = 39,6 kvar idag.
        original = copy.deepcopy(self.assessment)
        for weight, expected in [(1, 7.0), (.5, 5.5), (0, 4.0)]:
            result = self.calc(weights={self.tx["id"]: weight})
            self.assertEqual(result["adjusted_runway_months"], expected)
            self.assertEqual(result["runway_from_today_months"], 4.0)
            self.assertEqual(result["months_since_report"], 2.0)
            self.assertEqual(result["reported_runway_months"], 6)
            self.assertEqual(result["events"][0]["weight_source"], "manual")
            self.assertEqual(result["events"][0]["source_document_ids"], [3])
        self.assertEqual(self.assessment, original)

    def test_announced_financing_is_not_cash_by_default(self):
        result = self.calc()
        self.assertEqual(result["adjusted_runway_months"], 4.0)
        self.assertEqual(result["events"][0]["weight"], 0.0)
        self.assertEqual(result["events"][0]["weight_source"], "default_unconfirmed")

    def test_completed_financing_counts_in_full_by_default(self):
        result = self.calc([{**self.tx, "status": "completed"}])
        self.assertEqual(result["adjusted_runway_months"], 7.0)
        self.assertEqual(result["delta_runway_months"], 3.0)
        self.assertEqual(result["events"][0]["weight_source"], "default_confirmed")

    def test_canonical_group_counted_once(self):
        completed = {**self.tx, "status": "completed"}
        self.assertEqual(self.calc([completed, copy.deepcopy(completed)])["adjusted_runway_months"], 7.0)

    def test_pro_forma_comment_tracks_exact_weights_and_exclusions(self):
        included = self.calc(weights={self.tx["id"]: .5})
        self.assertEqual(included["pro_forma_cash_adjustment"], 15)
        self.assertIn("Runway från idag 5,5 mån", included["pro_forma_comment"])
        self.assertIn("2,0 mån sedan", included["pro_forma_comment"])
        self.assertIn("15 SEK vid 50 %", included["pro_forma_comment"])
        excluded = self.calc([{**self.tx, "status": "cancelled"}])
        self.assertIn("Runway från idag 4 mån", excluded["pro_forma_comment"])
        self.assertIn("Undantaget", excluded["pro_forma_comment"])

    def test_other_issuer_bridge_loan_cannot_change_comment_or_cash(self):
        result = self.calc([{**self.tx, "issuer_id": 2, "category": "bridge_loan"}])
        self.assertEqual(result["adjusted_runway_months"], 4.0)
        self.assertEqual(result["pro_forma_cash_adjustment"], 0)
        self.assertIn("Ingen post-report-finansiering", result["pro_forma_comment"])

    def test_cash_bridge_loan_is_included_when_same_release_mentions_setoff(self):
        bridge = {**self.tx, "id": "transaction:1:bridge_loan:3", "category": "bridge_loan",
                  "category_label": "Brygglån", "amount": 5_000_000, "status": "completed",
                  "_source_text": ("Bolaget har ingått ett brygglån om 5 MSEK. "
                                   "Företrädesemissionen omfattar även kvittning av andra skulder.")}
        assessment = {**self.assessment, "baseline_cash": 2_000_000,
                      "monthly_operating_burn": 1_000_000, "operating_runway_months": 2}
        result = calculate(assessment, [bridge], {}, as_of="2026-08-31")
        # Kassan är förbrukad idag (2 - 2,04 MSEK); brygglånet ger 5 MSEK.
        self.assertEqual(result["adjusted_runway_months"], 5.0)
        self.assertEqual(result["pro_forma_cash_adjustment"], 5_000_000)
        self.assertIn("Brygglån 5 MSEK vid 100 %", result["pro_forma_comment"])

    def test_noncash_continuation_and_noncash_bridge_are_excluded(self):
        equity = {**self.tx, "_source_text": (
            "Bolaget genomför en företrädesemission om 30 SEK. "
            "Betalning sker uteslutande genom kvittning."
        )}
        bridge = {**self.tx, "id": "transaction:1:bridge_loan:4", "category": "bridge_loan",
                  "category_label": "Brygglån", "_source_text": (
                      "Brygglån om 30 SEK ersätter befintliga skulder genom kvittning. "
                      "Ingen kontant likvid tillförs."
                  )}
        for transaction in (equity, bridge):
            with self.subTest(category=transaction["category"]):
                result = self.calc([transaction])
                self.assertEqual(result["pro_forma_cash_adjustment"], 0)
                self.assertIsNotNone(result["events"][0]["exclusion_reason"])

    def test_excludes_non_cash_mixed_currency_cancelled_and_future_information(self):
        variants = [dict(_source_text="Betalning genom kvittning"),
                    dict(_source_text="10 MSEK och 2 MEUR"), dict(currency="EUR"),
                    dict(status="cancelled"), dict(amount=None),
                    dict(history=self.tx["history"] + [dict(published_at="2026-09-02", title="Utfall")])]
        for fields in variants:
            with self.subTest(fields=fields):
                result = self.calc([{**self.tx, "status": "completed", **fields}])
                self.assertEqual(result["adjusted_runway_months"], 4.0)
                self.assertTrue(result["events"][0]["exclusion_reason"])

    def test_report_included_and_future_announcements_do_not_adjust(self):
        for day in ["2026-06-30", "2026-09-01"]:
            tx = {**self.tx, "history": [dict(published_at=day, title="Nyemission")]}
            self.assertEqual(self.calc([tx])["events"], [])

    def test_invalid_weights_rejected(self):
        for weight in [True, float("nan"), 2, -1, "0.5"]:
            with self.assertRaises(ValueError):
                self.calc(weights={self.tx["id"]: weight})

    def test_month_end_capture_is_immutable_and_idempotent(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        instant = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
        with patch("capital_radar.radar_analysis.datetime") as clock, patch("capital_radar.radar_analysis.current_analysis", return_value=[self.calc()]):
            clock.now.return_value = instant
            self.assertEqual(capture_month_end(connection), 1)
            self.assertEqual(capture_month_end(connection), 0)
            with self.assertRaises(ValueError):
                capture_month_end(connection, now=datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc))
        stored = json.loads(connection.execute("SELECT calculation_json FROM issuer_runway_snapshots").fetchone()[0])
        self.assertEqual(stored["events"][0]["weight"], 0.0)
        for sql in ["UPDATE issuer_runway_snapshots SET cash=999", "DELETE FROM issuer_runway_snapshots"]:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql)



if __name__ == "__main__":
    unittest.main()
