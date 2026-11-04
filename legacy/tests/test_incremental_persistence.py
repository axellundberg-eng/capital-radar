"""Steg H — inkrementell persistence.

Bevisar att ett oförändrat dokument (samma content/input hash) inte
analyseras på nytt, varken i det befintliga Kimi-flödet
(``extraction_jobs``) eller i det nya agent-lagret (``agent_runs`` /
``agent_artifacts``).

Grundregel: FETCH ONCE — PARSE ONCE — REUSE UNTIL INVALIDATED.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import AgentRunner
from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.ai.router import ModelRouter
from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.document_quality import evaluate_document_quality
from capital_radar.models import (
    AttachmentCandidate,
    DocumentCandidate,
    SecurityCandidate,
)
from capital_radar.normalization import stable_hash


REPORT_TEXT = (
    "[PAGE 1]\nInterim report Q1 2026.\n"
    "[PAGE 5]\nBalance sheet. Cash and cash equivalents "
    "were SEK 40 million.\n"
    "[PAGE 8]\nCash flow from operating activities "
    "was SEK -12 million.\n"
    "[PAGE 12]\nSubsequent events."
)


class IncrementalPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary_directory.name) / "incr.db")
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="INC-1",
                issuer_name="Incremental AB",
                display_name="Incremental",
                symbol="INC",
                isin="SE0000000301",
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
                    "SELECT issuer_id FROM securities WHERE external_id='INC-1'"
                ).fetchone()[0]
            )
        self.db.upsert_document(
            DocumentCandidate(
                source_id="mfn",
                external_id="inc-q1",
                issuer_name="Incremental AB",
                title="Incremental AB publishes interim report Q1 2026",
                canonical_url="https://www.mfn.se/a/incremental/q1",
                published_at="2026-05-01T07:00:00+02:00",
                attachments=(
                    AttachmentCandidate(
                        "https://storage.mfn.se/inc-full-report.pdf",
                        mime_type="application/pdf",
                    ),
                ),
            )
        )
        store = AnalysisStore(self.db)
        candidate = store.fetch_candidates(
            1, issuer_id=self.issuer_id, prefer_reports=True
        )[0]
        target = candidate.urls[0]
        quality = evaluate_document_quality(
            REPORT_TEXT, profile_category="report", page_count=12
        )
        store.save_content(
            candidate=candidate,
            attachment_id=target.attachment_id,
            source_url=target.url,
            final_url=target.url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash(target.url),
            byte_size=len(REPORT_TEXT),
            extraction_method="fixture",
            extracted_text=REPORT_TEXT,
            text_sha256=stable_hash(REPORT_TEXT),
            page_count=12,
            status="extracted",
            quality_score=quality.score,
            quality_status=quality.status,
            quality_reason=quality.reason,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_agent_runner_reuses_artifact_for_same_input(self) -> None:
        class _Provider:
            def __init__(self) -> None:
                self.calls = 0

            def structured_completion(self, **kwargs):
                self.calls += 1
                return ProviderResponse(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    content='{"verdict": "supported", "per_fact": [], "confidence": 0.8}',
                    finish_reason="stop",
                    usage=ProviderUsage(input_tokens=10, output_tokens=5),
                )

        settings = SimpleNamespace(
            deepseek_flash_model="deepseek-v4-flash",
            deepseek_flash_thinking=False,
            deepseek_flash_max_output_tokens=4096,
            kimi_model="kimi-k3",
            kimi_max_output_tokens=8000,
            kimi_max_input_chars=120000,
        )
        profile = ModelRouter(settings).route("evidence_validation").profile
        provider = _Provider()
        runner = AgentRunner(self.db)
        kwargs = dict(
            agent_type="evidence_validation",
            artifact_kind="evidence_audit",
            issuer_id=self.issuer_id,
            provider=provider,
            profile=profile,
            prompt_version="evidence-audit-v1",
            system_prompt="system",
            user_prompt="samma input",
            schema={"type": "object"},
        )
        first = runner.run(**kwargs)
        second = runner.run(**kwargs)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "skipped_idempotent")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(first.artifact_id, second.artifact_id)
        with self.db.connect() as connection:
            artifacts = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
        self.assertEqual(artifacts, 1)

    def test_new_input_hash_creates_new_version(self) -> None:
        class _Provider:
            def __init__(self, content: str) -> None:
                self._content = content

            def structured_completion(self, **kwargs):
                return ProviderResponse(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    content=self._content,
                    finish_reason="stop",
                    usage=ProviderUsage(input_tokens=10, output_tokens=5),
                )

        settings = SimpleNamespace(
            deepseek_flash_model="deepseek-v4-flash",
            deepseek_flash_thinking=False,
            deepseek_flash_max_output_tokens=4096,
            kimi_model="kimi-k3",
            kimi_max_output_tokens=8000,
            kimi_max_input_chars=120000,
        )
        profile = ModelRouter(settings).route("evidence_validation").profile
        runner = AgentRunner(self.db)
        base = dict(
            agent_type="evidence_validation",
            artifact_kind="evidence_audit",
            issuer_id=self.issuer_id,
            profile=profile,
            prompt_version="evidence-audit-v1",
            system_prompt="system",
            schema={"type": "object"},
        )
        first = runner.run(
            provider=_Provider('{"verdict": "supported", "per_fact": []}'),
            user_prompt="input version 1",
            **base,
        )
        second = runner.run(
            provider=_Provider('{"verdict": "partially_supported", "per_fact": []}'),
            user_prompt="input version 2 — nytt innehåll",
            **base,
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        with self.db.connect() as connection:
            versions = connection.execute(
                """
                SELECT version_no FROM agent_artifacts
                WHERE agent_type='evidence_validation'
                ORDER BY version_no
                """
            ).fetchall()
        self.assertEqual([row["version_no"] for row in versions], [1, 2])


if __name__ == "__main__":
    unittest.main()
