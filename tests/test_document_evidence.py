from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from capital_radar.analysis_store import AnalysisStore, FetchCandidate
from capital_radar.db import Database
from capital_radar.evidence_store import document_evidence, evidence_contains, archive_document_evidence
from capital_radar.models import DocumentCandidate


class DocumentEvidenceTests(unittest.TestCase):
    def test_page_markers_are_not_source_text_and_valid_offsets_are_preserved(self):
        with self.db.connect() as c:
            for text in ['', ' \n\t', '[PAGE 1]\n\n\n[PAGE 2]', '[page 1]\n---\n[page 2]']:
                with self.subTest(text=text):
                    self.assertIsNone(archive_document_evidence(
                        c, document_id=self.document_id, content_id=None, text=text))
            # Failed extraction artifacts remain auditable; none is ready.
            self.assertEqual(c.execute('SELECT COUNT(*) FROM document_evidence_texts').fetchone()[0], 3)
            self.assertFalse(document_evidence(c,self.document_id)['available'])
            self.assertEqual(document_evidence(c,self.document_id)['evidence_status'],'unusable_text')
            text='[PAGE 1]\nÅäö α 😀\n[PAGE 2]\nPris 0,40 SEK.'
            eid=archive_document_evidence(c, document_id=self.document_id, content_id=None, text=text)
            self.assertIsNotNone(eid)
            payload=document_evidence(c,self.document_id)
            self.assertEqual(payload['pages'][0]['text'],text)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "evidence.db")
        self.db.initialize()
        self.db.upsert_document(DocumentCandidate(
            source_id="mfn", external_id="evidence-1", issuer_name="SciBase AB",
            title="Villkor i företrädesemission", canonical_url="https://example.test/scibase",
            published_at="2026-09-01T08:00:00Z", language="sv",
        ), universe_only=False)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id,issuer_id FROM documents WHERE canonical_url='https://example.test/scibase'"
            ).fetchone()
            self.document_id, self.issuer_id = int(row[0]), int(row[1])

    def test_full_text_and_page_offsets_survive_raw_content_purge(self):
        text = ("[PAGE 1]\nInledning.\n[PAGE 2]\n"
                "Garantiersättning är 10 procent kontant eller 12 procent i aktier.")
        candidate = FetchCandidate(
            document_id=self.document_id, issuer_id=self.issuer_id, issuer_name="SciBase AB",
            title="Villkor", published_at="2026-09-01T08:00:00Z",
            profile_category="other", language="sv", urls=(),
        )
        store = AnalysisStore(self.db)
        store.save_content(
            candidate=candidate, attachment_id=None, source_url="https://example.test/scibase",
            final_url=None, local_path=None, mime_type="text/plain", sha256=None,
            byte_size=len(text), extraction_method="fixture", extracted_text=text,
            text_sha256=None, page_count=2, status="extracted",
        )
        store.purge_single_document_content(self.document_id)
        with self.db.connect() as connection:
            payload = document_evidence(connection, self.document_id, page=2)
            self.assertTrue(evidence_contains(connection, self.document_id, "10 procent kontant"))
        self.assertTrue(payload["available"])
        self.assertEqual(payload["pages"][0]["page_number"], 2)
        self.assertGreater(payload["pages"][0]["start_offset"], 0)
        self.assertIn("12 procent i aktier", payload["pages"][0]["text"])


if __name__ == "__main__":
    unittest.main()
