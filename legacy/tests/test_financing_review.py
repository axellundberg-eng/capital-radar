import tempfile
import unittest
from pathlib import Path

from capital_radar.db import Database
from capital_radar.financing_review import (
    FinancingProposalError,
    create_proposal,
    list_proposals,
    resolve_proposal,
)


class FinancingReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "review.db")
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO issuers(canonical_name,normalized_name,created_at,updated_at) VALUES('Testbolag','testbolag','now','now')"
            )
            self.issuer_id = connection.execute("SELECT id FROM issuers WHERE normalized_name='testbolag'").fetchone()[0]
            connection.execute(
                """INSERT INTO documents(issuer_id,event_key,canonical_url,title,published_at,profile_category,cash_relevance_score,relevance_reason,created_at,updated_at)
                   VALUES(?, 'review-doc', 'https://example.test/release', 'Villkor för emission', '2026-09-21', 'financing', 10, 'test', 'now', 'now')""",
                (self.issuer_id,),
            )
            self.document_id = connection.execute("SELECT id FROM documents WHERE event_key='review-doc'").fetchone()[0]
            excerpt = "Garantiersättningen uppgår till 9 procent av garantiåtagandet."
            connection.execute(
                """INSERT INTO document_contents(document_id,source_url,extracted_text,status,created_at,updated_at)
                   VALUES(?, 'https://example.test/release', ?, 'extracted', 'now', 'now')""",
                (self.document_id, "Inledning. " + excerpt + " Slut."),
            )
            connection.execute(
                """INSERT INTO financing_events(issuer_id,event_fingerprint,event_type,description,status,cash_direction,liquidity_effect_kind,reflected_in_report,confidence,created_at,updated_at)
                   VALUES(?, 'review-event', 'share_issue', 'Emission', 'announced', 'inflow', 'potential', 'unknown', 0.8, 'now', 'now')""",
                (self.issuer_id,),
            )
            self.event_id = connection.execute("SELECT id FROM financing_events WHERE event_fingerprint='review-event'").fetchone()[0]
            self.excerpt = excerpt

    def tearDown(self):
        self.directory.cleanup()

    def payload(self):
        return {
            "issuer_id": self.issuer_id,
            "financing_event_id": self.event_id,
            "field_name": "guarantee_fee_pct",
            "proposed_value": 0.09,
            "source_document_id": self.document_id,
            "source_excerpt": self.excerpt,
            "confidence": 0.94,
            "rationale": "Källan anger garantiersättningen uttryckligen.",
        }

    def test_create_is_source_bound_and_idempotent(self):
        with self.database.connect() as connection:
            proposal = create_proposal(connection, self.payload(), actor="test")
            duplicate = create_proposal(connection, self.payload(), actor="test")
            self.assertEqual(proposal["id"], duplicate["id"])
            self.assertEqual(list_proposals(connection)["counts"], {"proposed": 1})
            invalid = self.payload() | {"source_excerpt": "saknas i dokumentet"}
            with self.assertRaises(FinancingProposalError):
                create_proposal(connection, invalid, actor="test")

    def test_apply_populates_empty_field_and_preserves_evidence(self):
        with self.database.connect() as connection:
            proposal = create_proposal(connection, self.payload(), actor="test")
            result = resolve_proposal(connection, proposal["id"], decision="apply", actor="reviewer")
            self.assertEqual(result["status"], "applied")
            value = connection.execute("SELECT guarantee_fee_pct FROM financing_events WHERE id=?", (self.event_id,)).fetchone()[0]
            self.assertAlmostEqual(value, 0.09)
            evidence = connection.execute("SELECT source_document_id FROM financing_event_enrichment_facts WHERE financing_event_id=?", (self.event_id,)).fetchone()[0]
            self.assertEqual(evidence, self.document_id)

    def test_apply_never_overwrites_existing_financial_value(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE financing_events SET guarantee_fee_pct=0.05 WHERE id=?", (self.event_id,))
            proposal = create_proposal(connection, self.payload(), actor="test")
            with self.assertRaises(FinancingProposalError):
                resolve_proposal(connection, proposal["id"], decision="apply", actor="reviewer")


if __name__ == "__main__":
    unittest.main()
