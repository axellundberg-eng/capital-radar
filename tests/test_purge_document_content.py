"""Tester för purge av dokumentrådata (extracted_text + PDF).

Root cause för Railway ~2 GB-tillväxt: ``document_contents.extracted_text``
och PDF-filer under ``data/documents/`` växte obegränsat eftersom purge bara
skedde för bolag som nått hela tolkningskedjan ("completed"). Fixen purgar
rådata direkt efter att extraction persisterat financial_facts —
``purge_single_document_content`` raderar ETT dokument så att parallella
workers som bearbetar andra dokument inte påverkas.

Dessa tester verifierar att:
- extracted_text och local_path nollställs
- PDF-filen raderas från disk
- document-metadata och source_url ALDRIG raderas
- metoden är idempotent (säker att köra två gånger)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.models import (
    AttachmentCandidate,
    DocumentCandidate,
    SecurityCandidate,
)
from capital_radar.normalization import stable_hash


class PurgeDocumentContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.db = Database(base / "purge.db")
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="PURGE-1",
                issuer_name="Purge AB",
                display_name="Purge",
                symbol="PURGE",
                isin="SE0000000999",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        with self.db.connect() as connection:
            self.issuer_id = int(
                connection.execute(
                    """
                    SELECT issuer_id FROM securities
                    WHERE source_id='nasdaq_nordic' AND external_id='PURGE-1'
                    """
                ).fetchone()[0]
            )
        self.store = AnalysisStore(self.db)
        self.documents_path = base / "documents"
        self.documents_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _add_document_with_content(self, with_file: bool = True) -> int:
        self.db.upsert_document(
            DocumentCandidate(
                source_id="mfn",
                external_id="purge-doc-1",
                issuer_name="Purge AB",
                title="Purge AB Q1",
                canonical_url="https://www.mfn.se/a/purge/q1",
                published_at="2026-05-01T07:00:00+02:00",
                attachments=(
                    AttachmentCandidate(
                        "https://storage.mfn.se/purge-q1.pdf",
                        mime_type="application/pdf",
                    ),
                ),
            )
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET queued_for_analysis=1 WHERE title='Purge AB Q1'"
            )
            document_id = int(
                connection.execute(
                    "SELECT id FROM documents WHERE title='Purge AB Q1'"
                ).fetchone()[0]
            )
        candidate = next(
            c
            for c in self.store.fetch_candidates(50, issuer_id=self.issuer_id)
            if c.document_id == document_id
        )
        local_path = None
        if with_file:
            pdf = self.documents_path / "purge-q1.pdf"
            pdf.write_bytes(b"%PDF-1.4 fixture")
            local_path = str(pdf)
        text = "[PAGE 1]\nCash SEK 10m. Operating cash flow SEK -2m."
        self.store.save_content(
            candidate=candidate,
            attachment_id=candidate.urls[0].attachment_id,
            source_url=candidate.urls[0].url,
            final_url=candidate.urls[0].url,
            local_path=local_path,
            mime_type="application/pdf",
            sha256=stable_hash("purge-doc-1"),
            byte_size=len(text),
            extraction_method="fixture",
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=1,
            status="extracted",
            quality_score=1.0,
            quality_status="ok",
            quality_reason="fixture",
        )
        return document_id

    def test_purge_removes_text_and_file_but_keeps_metadata(self) -> None:
        document_id = self._add_document_with_content(with_file=True)
        pdf = self.documents_path / "purge-q1.pdf"
        self.assertTrue(pdf.is_file())

        result = self.store.purge_single_document_content(document_id)

        self.assertEqual(result["contents_purged"], 1)
        self.assertEqual(result["files_deleted"], 1)
        self.assertFalse(pdf.is_file(), "PDF-filen ska vara raderad")

        with self.db.connect() as connection:
            content = connection.execute(
                """
                SELECT local_path, extracted_text, byte_size, char_count
                FROM document_contents WHERE document_id=?
                """,
                (document_id,),
            ).fetchone()
            self.assertIsNone(content[0], "local_path ska nollställas")
            self.assertIsNone(content[1], "extracted_text ska nollställas")
            self.assertIsNone(content[2], "byte_size ska nollställas")
            self.assertEqual(content[3], 0, "char_count ska nollställas")

            # Document-metadata och source_url får ALDRIG raderas.
            document = connection.execute(
                "SELECT id, canonical_url, title FROM documents WHERE id=?",
                (document_id,),
            ).fetchone()
            self.assertIsNotNone(document, "dokument-raden ska finnas kvar")
            self.assertEqual(
                document[1],
                "https://www.mfn.se/a/purge/q1",
                "source_url ska bevaras",
            )
            content_row = connection.execute(
                "SELECT source_url FROM document_contents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            self.assertEqual(
                content_row[0],
                "https://storage.mfn.se/purge-q1.pdf",
                "content source_url ska bevaras",
            )

    def test_purge_is_idempotent_and_safe_without_file(self) -> None:
        document_id = self._add_document_with_content(with_file=False)

        first = self.store.purge_single_document_content(document_id)
        second = self.store.purge_single_document_content(document_id)

        self.assertEqual(first["contents_purged"], 1)
        self.assertEqual(first["files_deleted"], 0)
        # Andra körningen: inget kvar att rensa, men får inte krascha.
        self.assertEqual(second["files_deleted"], 0)

    def test_purged_document_is_refetched(self) -> None:
        """Ett purgat MFN-dokument (text NULL) ska hämtas om.

        Root cause för "hämtar inte om MFN-nyheterna": fetch_candidates
        exkluderade alla dokument med en content-rad status='extracted' utan
        att kolla om extracted_text fanns kvar. Efter purge är texten NULL,
        så dokumentet måste plockas upp för omhämtning igen.
        """
        document_id = self._add_document_with_content(with_file=False)

        # Innan purge: dokumentet har text och ska INTE vara en fetch-kandidat.
        before = {
            c.document_id
            for c in self.store.fetch_candidates(50, issuer_id=self.issuer_id)
        }
        self.assertNotIn(
            document_id,
            before,
            "dokument med text ska inte hämtas om",
        )

        self.store.purge_single_document_content(document_id)

        # Efter purge: texten är NULL, så dokumentet MÅSTE hämtas om.
        after = {
            c.document_id
            for c in self.store.fetch_candidates(50, issuer_id=self.issuer_id)
        }
        self.assertIn(
            document_id,
            after,
            "purgat dokument (text NULL) ska hämtas om",
        )

    def test_purged_document_excluded_from_analysis_candidates(self) -> None:
        """Ett purgat dokument (text NULL) ska inte matas till AI:n.

        Annars skulle str(None) → bokstavliga "None" skickas som dokumenttext
        och modellen svarar "Fakta saknas".
        """
        document_id = self._add_document_with_content(with_file=False)

        before = {
            c.document_id
            for c in self.store.analysis_candidates(50, issuer_id=self.issuer_id)
        }
        self.assertIn(
            document_id,
            before,
            "dokument med text ska vara en analyskandidat",
        )

        self.store.purge_single_document_content(document_id)

        after = {
            c.document_id
            for c in self.store.analysis_candidates(50, issuer_id=self.issuer_id)
        }
        self.assertNotIn(
            document_id,
            after,
            "purgat dokument (text NULL) ska inte analyseras",
        )

    def test_purge_does_not_touch_other_documents(self) -> None:
        document_id = self._add_document_with_content(with_file=False)
        # Ett annat dokument för samma issuer med egen text.
        self.db.upsert_document(
            DocumentCandidate(
                source_id="mfn",
                external_id="purge-doc-2",
                issuer_name="Purge AB",
                title="Purge AB Q2",
                canonical_url="https://www.mfn.se/a/purge/q2",
                published_at="2026-08-01T07:00:00+02:00",
                attachments=(
                    AttachmentCandidate(
                        "https://storage.mfn.se/purge-q2.pdf",
                        mime_type="application/pdf",
                    ),
                ),
            )
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET queued_for_analysis=1 WHERE title='Purge AB Q2'"
            )
            other_id = int(
                connection.execute(
                    "SELECT id FROM documents WHERE title='Purge AB Q2'"
                ).fetchone()[0]
            )
        other_candidate = next(
            c
            for c in self.store.fetch_candidates(50, issuer_id=self.issuer_id)
            if c.document_id == other_id
        )
        self.store.save_content(
            candidate=other_candidate,
            attachment_id=other_candidate.urls[0].attachment_id,
            source_url=other_candidate.urls[0].url,
            final_url=other_candidate.urls[0].url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash("purge-doc-2"),
            byte_size=10,
            extraction_method="fixture",
            extracted_text="annan text",
            text_sha256=stable_hash("annan text"),
            page_count=1,
            status="extracted",
        )

        self.store.purge_single_document_content(document_id)

        with self.db.connect() as connection:
            other = connection.execute(
                "SELECT extracted_text FROM document_contents WHERE document_id=?",
                (other_id,),
            ).fetchone()
            self.assertEqual(
                other[0],
                "annan text",
                "and dokumentets text får inte påverkas av per-dokument-purge",
            )

    def test_purge_raw_content_bulk_extracted(self) -> None:
        """Bulk-purge ska rensa rådata för extraherade dokument."""
        document_id = self._add_document_with_content(with_file=False)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET analysis_status='extracted' WHERE id=?",
                (document_id,),
            )
            connection.commit()

        result = self.store.purge_raw_content_bulk(
            older_than_days=0, include_extracted=True
        )

        self.assertEqual(result["contents_purged"], 1)
        self.assertEqual(result["files_deleted"], 0)
        self.assertTrue(result["include_extracted"])

        with self.db.connect() as connection:
            content = connection.execute(
                "SELECT extracted_text FROM document_contents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            self.assertIsNone(content[0], "extracted_text ska vara NULL efter bulk-purge")

    def test_purge_raw_content_bulk_old_metadata_only(self) -> None:
        """Bulk-purge ska rensa gamla metadata_only-dokument."""
        document_id = self._add_document_with_content(with_file=False)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET analysis_status='metadata_only', published_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (document_id,),
            )
            connection.commit()

        result = self.store.purge_raw_content_bulk(
            older_than_days=7, include_extracted=False
        )

        self.assertEqual(result["contents_purged"], 1)
        self.assertFalse(result["include_extracted"])

        with self.db.connect() as connection:
            content = connection.execute(
                "SELECT extracted_text FROM document_contents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            self.assertIsNone(content[0], "gammal metadata_only-text ska rensas")

    def test_purge_raw_content_bulk_keeps_content_ready(self) -> None:
        """Bulk-purge ska INTE rensa content_ready-dokument (de ska analyseras)."""
        document_id = self._add_document_with_content(with_file=False)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET analysis_status='content_ready', published_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (document_id,),
            )
            connection.commit()

        result = self.store.purge_raw_content_bulk(
            older_than_days=7, include_extracted=False
        )

        self.assertEqual(result["contents_purged"], 0)

        with self.db.connect() as connection:
            content = connection.execute(
                "SELECT extracted_text FROM document_contents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            self.assertIsNotNone(content[0], "content_ready-text ska bevaras")


if __name__ == "__main__":
    unittest.main()
