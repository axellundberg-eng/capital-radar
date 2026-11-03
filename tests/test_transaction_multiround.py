from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.models import DocumentCandidate
from capital_radar.transaction_feed import get_transaction_feed


class MultiRoundFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(".tmp-tests")
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "multi-round.db")
        self.db.initialize()

    def document(self, title: str, number: int, day: int) -> int:
        self.db.upsert_document(DocumentCandidate(
            source_id="mfn", external_id=str(number), issuer_name="Round Fixture AB",
            title=title, canonical_url=f"https://mfn.se/a/round-fixture/{number}",
            published_at=f"2026-09-{day:02d}T09:00:00Z", language="sv",
        ), universe_only=False)
        with self.db.connect() as connection:
            return int(connection.execute(
                "SELECT document_id FROM document_sources WHERE source_id='mfn' AND source_external_id=?",
                (str(number),),
            ).fetchone()["document_id"])

    def test_one_document_can_support_two_instruments(self) -> None:
        doc = self.document(
            "Round Fixture: TO13 teckningsoptioner 32,4 MSEK och separat låneavtal 50 MSEK",
            1, 15,
        )
        links = {doc: [
            {"canonical_round_key": "to13_2026", "category": "warrant_exercise",
             "financing_event_id": None, "transaction_id": None, "correction_id": 1},
            {"canonical_round_key": "loan_2026", "category": "other_debt",
             "financing_event_id": None, "transaction_id": None, "correction_id": 2},
        ]}
        with patch("capital_radar.transaction_feed.effective_document_round_links", return_value=links):
            with self.db.connect() as connection:
                items = get_transaction_feed(connection)["items"]
        self.assertEqual({item["category"] for item in items}, {"warrant_exercise", "other_debt"})
        self.assertEqual(len({item["id"] for item in items}), 2)
        self.assertTrue(all(item["source_document_ids"] == [doc] for item in items))
        self.assertEqual({
            item["category"]: item["details"]["financing_dossier"]["canonical_round_key"]
            for item in items
        }, {"warrant_exercise": "to13_2026", "other_debt": "loan_2026"})

    def test_same_document_two_rounds_have_distinct_ids(self) -> None:
        doc = self.document("Round Fixture beslutar om riktad emission om 6 MSEK", 2, 16)
        with self.db.connect() as connection:
            issuer_id = int(connection.execute(
                "SELECT issuer_id FROM documents WHERE id=?", (doc,)
            ).fetchone()["issuer_id"])
        original_id = f"transaction:{issuer_id}:directed_issue:{doc}"
        links = {doc: [
            {"canonical_round_key": "placement_a", "category": "directed_issue",
             "financing_event_id": None, "transaction_id": original_id, "correction_id": 1},
            {"canonical_round_key": "placement_b", "category": "directed_issue",
             "financing_event_id": None, "transaction_id": None, "correction_id": 2},
        ]}
        with patch("capital_radar.transaction_feed.effective_document_round_links", return_value=links):
            with self.db.connect() as connection:
                items = get_transaction_feed(connection)["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["id"] for item in items}), 2)
        self.assertIn(original_id, {item["id"] for item in items})
        self.assertEqual({
            item["details"]["financing_dossier"]["canonical_round_key"]
            for item in items
        }, {"placement_a", "placement_b"})

    def test_final_outcome_amount_precedes_preliminary_and_announcement(self) -> None:
        documents = [
            self.document("Round Fixture beslutar om företrädesemission om 10 MSEK", 3, 1),
            self.document("Preliminärt utfall i Round Fixtures företrädesemission 9 MSEK", 4, 8),
            self.document("Slutligt utfall i Round Fixtures företrädesemission 8,5 MSEK", 5, 10),
            self.document("Uppdatering av Round Fixtures företrädesemission, teckningsavsikt 0,7 MSEK", 6, 12),
        ]
        links = {doc: [{"canonical_round_key": "rights_2026", "category": "rights_issue",
                        "financing_event_id": None, "transaction_id": None,
                        "correction_id": 3}] for doc in documents}
        with patch("capital_radar.transaction_feed.effective_document_round_links", return_value=links):
            with self.db.connect() as connection:
                items = get_transaction_feed(connection)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["amount"], 8_500_000)
        self.assertEqual(items[0]["amount_basis"], "outcome_amount")
        self.assertEqual(items[0]["source_document_ids"], sorted(documents))


if __name__ == "__main__":
    unittest.main()
