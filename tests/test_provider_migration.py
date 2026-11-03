from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisCandidate, AnalysisStore
from capital_radar.db import Database



class ProviderMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "provider.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _seed_issuer_document_content(self) -> None:
        """Insert minimal issuer/document/content rows into a live database."""
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO issuers(
                    canonical_name, normalized_name, created_at, updated_at
                ) VALUES ('Legacy AB', 'legacy ab', '2026-01-01', '2026-01-01')
                """
            )
            connection.execute(
                """
                INSERT INTO documents(
                    issuer_id, event_key, canonical_url, title, published_at,
                    relevance_reason, created_at, updated_at
                ) VALUES (
                    1, 'evt-1', 'https://example.com/r', 'Legacy report',
                    '2026-01-01', 'fixture', '2026-01-01', '2026-01-01'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO document_contents(
                    document_id, source_url, char_count, status,
                    created_at, updated_at
                ) VALUES (
                    1, 'https://example.com/r', 10, 'extracted',
                    '2026-01-01', '2026-01-01'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_create_job_accepts_explicit_provider(self) -> None:
        db = Database(self.path)
        db.initialize()
        self._seed_issuer_document_content()

        store = AnalysisStore(db)
        candidate = AnalysisCandidate(
            document_id=1,
            issuer_id=1,
            issuer_name="Legacy AB",
            title="Legacy report",
            published_at="2026-01-01",
            profile_category="report",
            language=None,
            content_id=1,
            source_url="https://example.com/r",
            extracted_text="some text",
            text_sha256="abc",
            page_count=1,
        )
        job_id, status, lease = store.create_job(
            candidate=candidate,
            model="deepseek-chat",
            prompt_version="capital-need-v2",
            input_sha256="input-1",
            selected_char_count=10,
            estimated_input_tokens=5,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="deepseek",
        )
        self.assertEqual(status, "created")
        self.assertIsNotNone(lease)

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT provider, model FROM extraction_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            self.assertEqual(row["provider"], "deepseek")
            self.assertEqual(row["model"], "deepseek-chat")
        finally:
            connection.close()

    def test_create_job_explicit_moonshot_provider_kept(self) -> None:
        db = Database(self.path)
        db.initialize()
        self._seed_issuer_document_content()

        store = AnalysisStore(db)
        candidate = AnalysisCandidate(
            document_id=1,
            issuer_id=1,
            issuer_name="Legacy AB",
            title="Legacy report",
            published_at="2026-01-01",
            profile_category="report",
            language=None,
            content_id=1,
            source_url="https://example.com/r",
            extracted_text="some text",
            text_sha256="abc",
            page_count=1,
        )
        job_id, _, _ = store.create_job(
            candidate=candidate,
            model="kimi-k3",
            prompt_version="capital-need-v1",
            input_sha256="input-1",
            selected_char_count=10,
            estimated_input_tokens=5,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="moonshot",
        )
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT provider FROM extraction_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            self.assertEqual(row["provider"], "moonshot")
        finally:
            connection.close()

    def test_historical_extraction_rows_remain_readable(self) -> None:
        """Historical provider='moonshot' rows stay readable with provenance intact."""
        db = Database(self.path)
        db.initialize()
        self._seed_issuer_document_content()

        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO extraction_jobs(
                    issuer_id, document_id, content_id, provider, model,
                    prompt_version, input_sha256, selected_char_count,
                    estimated_input_tokens, status, parsed_output_json,
                    attempt_count, started_at, completed_at
                ) VALUES (
                    1, 1, 1, 'moonshot', 'kimi-k3', 'capital-need-v1',
                    'input-legacy', 10, 5, 'completed',
                    '{"facts": [{"fact_type": "cash", "source_excerpt": "Cash SEK 40m"}]}',
                    1, '2026-01-01', '2026-01-01'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT provider, model, prompt_version FROM extraction_jobs"
            ).fetchone()
            # Provenance is immutable: still the original moonshot metadata.
            self.assertEqual(row["provider"], "moonshot")
            self.assertEqual(row["model"], "kimi-k3")
            self.assertEqual(row["prompt_version"], "capital-need-v1")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
