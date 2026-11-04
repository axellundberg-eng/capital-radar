from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.db import Database
from capital_radar.manual_actions import ManualActionService
from capital_radar.models import DocumentCandidate, SecurityCandidate


class MfnIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Database(Path(self.directory.name) / "synthetic.db")
        self.db.initialize()
        run = self.db.start_run("nasdaq_nordic", "universe", "synthetic")
        self.db.upsert_security(SecurityCandidate(
            source_id="nasdaq_nordic", external_id="synthetic-aira",
            issuer_name="AI Revenue Assistant Software",
            display_name="AI Revenue Assistant Software", symbol="AIRA",
            isin="SE0000000112", currency="SEK",
            market="Nasdaq First North Growth Market Sweden", segment="First North GM",
        ), run)
        with self.db.connect() as connection:
            self.issuer_id = int(connection.execute("SELECT id FROM issuers").fetchone()[0])

    def candidate(self, name: str, language: str = "sv", source: str = "mfn") -> DocumentCandidate:
        return DocumentCandidate(
            source_id=source, external_id=f"synthetic-{language}", issuer_name=name,
            title=("Aira genomför en riktad nyemission" if language == "sv"
                   else "Aira carries out a directed share issue"),
            canonical_url=f"https://www.mfn.se/a/aira/synthetic-{language}",
            published_at="2026-10-20T21:20:00+02:00", language=language,
        )

    def test_both_languages_reach_existing_listed_issuer_and_analysis_queue(self) -> None:
        for language in ("sv", "en"):
            self.assertEqual(self.db.upsert_document(self.candidate(
                "AI Revenue Assistant Software Stockholm AB (publ)", language
            )), ("created", True))
        with self.db.connect() as connection:
            rows = connection.execute("SELECT issuer_id, queued_for_analysis FROM documents").fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows], [(self.issuer_id, 1)] * 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM issuers").fetchone()[0], 1)

    def test_cold_start_uses_verified_mfn_slug_without_prior_documents(self) -> None:
        service = object.__new__(ManualActionService)
        service.db = self.db
        self.assertEqual(service._resolve_mfn_identity(
            self.issuer_id, "AI Revenue Assistant Software"
        ), ("aira", "https://www.mfn.se/all/a/aira"))

    def test_global_source_then_issuer_refresh_cannot_duplicate_document(self) -> None:
        self.assertEqual(self.db.upsert_document(self.candidate(
            "AI Revenue Assistant Software Stockholm"
        )), ("created", True))
        self.assertEqual(self.db.upsert_document(self.candidate(
            "AI Revenue Assistant Software"
        )), ("linked", True))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_sources").fetchone()[0], 1)

    def test_issuer_refresh_then_global_source_cannot_duplicate_document(self) -> None:
        self.assertEqual(self.db.upsert_document(self.candidate(
            "AI Revenue Assistant Software"
        )), ("created", True))
        self.assertEqual(self.db.upsert_document(self.candidate(
            "AI Revenue Assistant Software Stockholm"
        )), ("linked", True))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_sources").fetchone()[0], 1)

    def test_no_fuzzy_city_or_private_heat_pump_company_match(self) -> None:
        for name in ("Aira", "Aira Group AB", "AI Revenue Assistant Software Göteborg",
                     "Other Software Stockholm AB"):
            with self.subTest(name=name):
                self.assertEqual(self.db.upsert_document(self.candidate(name)), ("unmatched", False))

    def test_mapping_is_mfn_specific(self) -> None:
        self.assertEqual(self.db.upsert_document(self.candidate(
            "AI Revenue Assistant Software Stockholm", source="nasdaq_cns"
        )), ("unmatched", False))

    def test_wrong_ticker_inactive_or_wrong_market_cannot_use_binding(self) -> None:
        for column, value in (("symbol", "OTHER"), ("active", 0), ("market", "Other market")):
            with self.subTest(column=column):
                with self.db.connect() as connection:
                    previous = connection.execute(f"SELECT {column} FROM securities").fetchone()[0]
                    connection.execute(f"UPDATE securities SET {column}=?", (value,))
                self.assertEqual(self.db.upsert_document(self.candidate(
                    "AI Revenue Assistant Software Stockholm"
                )), ("unmatched", False))
                service = object.__new__(ManualActionService)
                service.db = self.db
                self.assertNotEqual(service._resolve_mfn_identity(
                    self.issuer_id, "AI Revenue Assistant Software"
                )[0], "aira")
                with self.db.connect() as connection:
                    connection.execute(f"UPDATE securities SET {column}=?", (previous,))

    def test_existing_exact_issuer_identity_takes_precedence(self) -> None:
        with self.db.connect() as connection:
            other = self.db._resolve_or_create_issuer(connection,
                "AI Revenue Assistant Software Stockholm", "mfn", None)
        self.assertNotEqual(other, self.issuer_id)
        self.db.upsert_document(self.candidate("AI Revenue Assistant Software Stockholm"))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT issuer_id FROM documents").fetchone()[0], other)


if __name__ == "__main__":
    unittest.main()
