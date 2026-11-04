from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.relevance import classify_title


class RelevanceTests(unittest.TestCase):
    def test_actual_report_is_queued(self) -> None:
        result = classify_title("Bolaget publicerar delårsrapport Q2 2026")
        self.assertTrue(result.queued_for_analysis)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.profile_category, "report")

    def test_invitation_to_report_presentation_is_not_queued(self) -> None:
        for title in (
            "Invitation to Presentation of Ranplan's H1 Interim Report 2026",
            "Invitation to AAC Clyde Space's Q1 2026 webcast",
            "Invitation to Upsales Q2 2026 webinar",
        ):
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertFalse(result.queued_for_analysis)
                self.assertEqual(result.score, 0)
                self.assertEqual(result.profile_category, "calendar")

    def test_plural_bridge_loans_are_queued(self) -> None:
        result = classify_title("Company raises bridge loans")
        self.assertTrue(result.queued_for_analysis)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.profile_category, "financing")

    def test_profit_warning_is_saved_but_not_sent_to_cash_analysis(self) -> None:
        result = classify_title("Bolaget lämnar vinstvarning inför Q3")
        self.assertFalse(result.queued_for_analysis)
        self.assertEqual(result.profile_category, "profit_warning")

    def test_new_commercial_agreement_is_profile_news_only(self) -> None:
        result = classify_title("Gaming Corps tecknar avtal med evoke plc")
        self.assertFalse(result.queued_for_analysis)
        self.assertEqual(result.profile_category, "commercial_deal")

    def test_swedish_half_year_report_is_queued(self) -> None:
        result = classify_title("Genovis AB Halvårsrapport januari–juni 2026")
        self.assertTrue(result.queued_for_analysis)
        self.assertEqual(result.profile_category, "report")

    def test_quarterly_report_is_always_cash_relevant(self) -> None:
        for title in (
            "Bolaget publicerar Kvartalsrapport 2026-Q2",
            "Company publishes Q2 2026 interim report",
            "Cell Impact Q1 2026: Measured progress in a turbulent market",
            "Rapport för det andra kvartalet 2026",
        ):
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertTrue(result.queued_for_analysis)
                self.assertEqual(result.score, 100)
                self.assertEqual(result.profile_category, "report")

    def test_definite_form_report_titles_are_queued(self) -> None:
        # Svenska bestämda formen ska klassas som rapport, inte falla utanför.
        for title in (
            "Oncopeptides publicerar årsredovisningen för 2025",
            "Bolaget publicerar delårsrapporten för andra kvartalet",
            "Kvartalsrapporten för Q2 2026 publicerad",
            "Halvårsrapporten januari–juni 2026",
            "Bokslutskommunikén 2025",
        ):
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertTrue(result.queued_for_analysis)
                self.assertEqual(result.score, 100)
                self.assertEqual(result.profile_category, "report")

    def test_previously_missed_report_title_variants_are_queued(self) -> None:
        # Titelformuleringar som tidigare klassificerades som 'other' trots att
        # dokumentet är en finansiell rapport (P3-täckningslucka).
        for title in (
            "Resultaträkning och balansräkning 2024",
            "Koncernredovisning 2024",
            "First quarter report 2025",
            "Second quarter results 2025",
            "Preliminärt bokslut 2024",
            "Bokslut 2024",
            "Finansiell rapport Q2",
            "Periodrapport 2025",
            "Rapport januari-december 2024",
            "Q3 rapport 2025",
            "Kvartal 2 rapport",
        ):
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertEqual(result.profile_category, "report")

    def test_financing_titles_are_not_reclassified_as_report(self) -> None:
        # Regressionsskydd: rapportreglerna får inte "äta" finansierings-PM.
        for title in (
            "Bolaget genomför en riktad nyemission om 55 MSEK",
            "Company resolves on a directed share issue",
            "Bolaget genomför en företrädesemission om 84 MSEK",
            "Bolaget tar upp ett brygglån om 15 MSEK",
        ):
            with self.subTest(title=title):
                result = classify_title(title)
                self.assertEqual(result.profile_category, "financing")


if __name__ == "__main__":
    unittest.main()
