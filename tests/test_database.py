from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.db import Database
from capital_radar.models import DocumentCandidate, SecurityCandidate


class DatabaseTests(unittest.TestCase):
    def test_cross_source_document_is_linked_and_queued_once(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            db = Database(path)
            db.initialize()
            run_id = db.start_run("nasdaq_nordic", "universe", "fixture")
            db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id="TX1",
                    issuer_name="Exempel B",
                    display_name="Exempel B",
                    symbol="EX B",
                    isin="SE0000000001",
                    currency="SEK",
                    market="Nasdaq Stockholm",
                    segment="Small Cap",
                ),
                run_id,
            )
            db.finish_run(run_id, status="success", items_seen=1, items_upserted=1)

            first = DocumentCandidate(
                source_id="mfn",
                external_id="mfn-1",
                issuer_name="Exempel AB (publ)",
                title="Exempel genomför riktad emission",
                canonical_url="https://mfn.se/a/exempel/emission",
                published_at="2026-07-23T09:00:00+02:00",
            )
            second = DocumentCandidate(
                source_id="nasdaq_cns",
                external_id="123",
                issuer_name="EXEMPEL AB",
                title="Exempel genomför riktad emission",
                canonical_url="https://view.news.eu.nasdaq.com/view?id=123",
                published_at="2026-07-23T09:00:00+02:00",
                language="sv",
            )
            self.assertEqual(db.upsert_document(first), ("created", True))
            self.assertEqual(db.upsert_document(second), ("linked", True))
            commercial = DocumentCandidate(
                source_id="mfn",
                external_id="mfn-2",
                issuer_name="Exempel AB",
                title="Exempel tecknar avtal med ny kund",
                canonical_url="https://mfn.se/a/exempel/new-customer",
                published_at="2026-07-24T09:00:00+02:00",
            )
            self.assertEqual(db.upsert_document(commercial), ("created", False))

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM document_sources"
                    ).fetchone()[0],
                    3,
                )
                score = connection.execute(
                    """
                    SELECT cash_relevance_score FROM documents
                    WHERE title LIKE '%riktad emission'
                    """
                ).fetchone()[0]
                self.assertEqual(score, 100)
                profile_category, queued = connection.execute(
                    """
                    SELECT profile_category, queued_for_analysis
                    FROM documents WHERE title LIKE '%ny kund'
                    """
                ).fetchone()
                self.assertEqual(profile_category, "commercial_deal")
                self.assertEqual(queued, 0)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
