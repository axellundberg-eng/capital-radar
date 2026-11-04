"""Tester för bounded-parallell extraction (canonical execution hot path).

Verifierar DONE WHEN-kraven för extraction:
- flera dokument processas parallellt (wall-clock < sekventiell tid)
- EN delad AsyncOpenAI-klient byggs per batch (aldrig client-per-dokument)
- concurrency är bounded och konfigurerbar
- idempotency sker före provider call (skipped_idempotent utan nätverk)
- batch failure isoleras per dokument
- executor metrics exponeras i resultatet
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisStore
from capital_radar.config import Settings
from capital_radar.db import Database
from capital_radar.extraction import analyze_documents
from capital_radar.models import (
    AttachmentCandidate,
    DocumentCandidate,
    SecurityCandidate,
)
from capital_radar.normalization import stable_hash


class _FakeClient:
    """Minimal ställvertäträdare för AsyncOpenAI — räknar close-anrop."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class ParallelExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary_directory.name) / "par.db")
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="PAR-1",
                issuer_name="Parallel AB",
                display_name="Parallel",
                symbol="PAR",
                isin="SE0000000102",
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
                    WHERE source_id='nasdaq_nordic' AND external_id='PAR-1'
                    """
                ).fetchone()[0]
            )
        self.store = AnalysisStore(self.db)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _settings(self, concurrency: int = 4) -> Settings:
        import dataclasses

        return dataclasses.replace(
            Settings.from_env(),
            deepseek_api_key="test-key",
            deepseek_flash_max_concurrency=concurrency,
            deepseek_flash_hard_timeout_seconds=30,
        )

    def _add_document(self, external_id: str, title: str, day: int = 1) -> None:
        self.db.upsert_document(
            DocumentCandidate(
                source_id="mfn",
                external_id=external_id,
                issuer_name="Parallel AB",
                title=title,
                canonical_url=f"https://www.mfn.se/a/parallel/{external_id}",
                published_at=f"2026-05-{day:02d}T07:00:00+02:00",
                attachments=(
                    AttachmentCandidate(
                        f"https://storage.mfn.se/{external_id}.pdf",
                        mime_type="application/pdf",
                    ),
                ),
            )
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE documents SET queued_for_analysis=1 WHERE title=?",
                (title,),
            )
            document_id = int(
                connection.execute(
                    "SELECT id FROM documents WHERE title=?", (title,)
                ).fetchone()[0]
            )
        candidate = next(
            c
            for c in self.store.fetch_candidates(50, issuer_id=self.issuer_id)
            if c.document_id == document_id
        )
        text = (
            "[PAGE 1]\nInterim report. Cash and cash equivalents "
            "were SEK 10 million. Cash flow from operating activities "
            "was SEK -2 million."
        )
        self.store.save_content(
            candidate=candidate,
            attachment_id=candidate.urls[0].attachment_id,
            source_url=candidate.urls[0].url,
            final_url=candidate.urls[0].url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash(external_id),
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

    def _fake_outcome(self, job_id: int) -> dict[str, Any]:
        return {
            "status": "completed",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cached_tokens": 0,
                "total_tokens": 110,
            },
            "model": "deepseek-v4-flash",
            "gate_decision": "accept",
            "artifact_id": 1,
            "provider_calls": 1,
        }

    def test_parallel_batch_shared_client_and_metrics(self) -> None:
        for index in range(5):
            self._add_document(
                f"par-q{index}", f"Parallel AB report {index}", day=index + 1
            )

        built_clients: list[_FakeClient] = []
        in_flight = 0
        peak = 0

        def _build(settings: Any) -> _FakeClient:
            client = _FakeClient()
            built_clients.append(client)
            return client

        async def _fake_extract(store, settings, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)  # simulerad provider-latens
            in_flight -= 1
            return self._fake_outcome(kwargs["job_id"])

        settings = self._settings(concurrency=4)
        with patch(
            "capital_radar.extraction.build_async_client", side_effect=_build
        ), patch(
            "capital_radar.extraction.run_extraction_v2_async",
            side_effect=_fake_extract,
        ):
            start = time.monotonic()
            result = analyze_documents(
                self.store, settings, limit=10, dry_run=False
            )
            elapsed = time.monotonic() - start

        self.assertEqual(result["completed"], 5, result["documents"])
        self.assertEqual(result["failed"], 0)
        # EN delad klient för hela batchen, stängd exakt en gång.
        self.assertEqual(len(built_clients), 1)
        self.assertEqual(built_clients[0].closed, 1)
        # Bounded concurrency: aldrig fler än 4 samtidiga, men > 1.
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        # Parallell vinst: 5 x 50ms sekventiellt = 250ms; parallellt ~100ms.
        # Gränsen ligger strax under den sekventiella tiden så att testet
        # bevisar att körningen INTE var sekventiell, men tål CPU-brus när
        # hela sviten (med flera parallella tråd-tester) kör samtidigt.
        self.assertLess(elapsed, 0.25)
        # Executor metrics finns och räknar exakt.
        metrics = result["executor_metrics"]
        self.assertEqual(metrics["completed"], 5)
        self.assertEqual(metrics["provider_calls"], 5)
        self.assertEqual(metrics["input_tokens"], 500)

    def test_idempotency_skips_before_provider(self) -> None:
        self._add_document("par-idem", "Parallel AB idempotent report")
        calls = {"n": 0}

        async def _fake_extract(store, settings, **kwargs):
            calls["n"] += 1
            # Simulera att providern svarat och jobbet persisterats completed.
            # VIKTIGT: minst ett fact krävs för att jobbet inte ska återtas
            # av "0 facts"-kontrollen i create_job.
            store.complete_job(
                kwargs["job_id"],
                kwargs["lease_token"],
                response_id="fake-response",
                finish_reason="stop",
                usage={},
                raw_response={"fake": True},
                parsed_output={
                    "report": {
                        "is_financial_report": True,
                        "period_start": "2026-01-01",
                        "period_end": "2026-03-31",
                        "publication_date": "2026-05-01",
                        "currency": "SEK",
                        "reported_scale": "millions",
                        "period_months": 3,
                        "confidence": 0.9,
                        "facts": [
                            {
                                "fact_type": "cash_and_cash_equivalents",
                                "reported_value": 10.0,
                                "normalized_value": 10.0,
                                "currency": "SEK",
                                "reported_unit": "millions",
                                "period_start": "2026-01-01",
                                "period_end": "2026-03-31",
                                "as_of_date": "2026-03-31",
                                "evidence_label": "stated",
                                "source_page": 1,
                                "source_excerpt": "Cash and cash equivalents were SEK 10 million",
                                "confidence": 0.9,
                            }
                        ],
                    },
                    "quality": {
                        "gate_decision": "accept",
                        "gate_signals": [],
                        "requires_human_review": False,
                        "uncertainties": [],
                    },
                    "financing_events": [],
                },
            )
            return self._fake_outcome(kwargs["job_id"])

        settings = self._settings()
        with patch(
            "capital_radar.extraction.build_async_client",
            side_effect=lambda s: _FakeClient(),
        ), patch(
            "capital_radar.extraction.run_extraction_v2_async",
            side_effect=_fake_extract,
        ):
            first = analyze_documents(
                self.store, settings, limit=10, dry_run=False
            )
            second = analyze_documents(
                self.store, settings, limit=10, dry_run=False
            )

        self.assertEqual(first["completed"], 1)
        self.assertEqual(second["skipped_idempotent"], 1)
        self.assertEqual(calls["n"], 1, "andra körningen får inte nå providern")

    def test_twenty_jobs_bounded_and_exact_request_count(self) -> None:
        """20 jobb / concurrency 4 = max 4 samtidiga, max 20 requests totalt."""
        for index in range(20):
            self._add_document(
                f"par-b{index}", f"Parallel AB batch report {index}",
                day=index + 1,
            )

        in_flight = 0
        peak = 0
        requests = {"n": 0}

        async def _fake_extract(store, settings, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            requests["n"] += 1  # 1 job = 1 request i fake:n
            await asyncio.sleep(0.02)
            in_flight -= 1
            return self._fake_outcome(kwargs["job_id"])

        settings = self._settings(concurrency=4)
        with patch(
            "capital_radar.extraction.build_async_client",
            side_effect=lambda s: _FakeClient(),
        ), patch(
            "capital_radar.extraction.run_extraction_v2_async",
            side_effect=_fake_extract,
        ):
            result = analyze_documents(
                self.store, settings, limit=50, dry_run=False
            )

        self.assertEqual(result["completed"], 20, result["documents"])
        self.assertLessEqual(peak, 4, "aldrig fler samtidiga än concurrency")
        self.assertGreater(peak, 1)
        self.assertEqual(requests["n"], 20, "exakt 1 request per job")
        self.assertEqual(result["executor_metrics"]["provider_calls"], 20)

    def test_failure_isolated_per_document(self) -> None:
        self._add_document("par-ok", "Parallel AB ok report", day=1)
        self._add_document("par-bad", "Parallel AB broken report", day=2)

        async def _fake_extract(store, settings, **kwargs):
            candidate = kwargs["candidate"]
            if "broken" in candidate.title:
                return {
                    "status": "failed",
                    "error": "provider timeout",
                    "usage": {},
                    "provider_calls": 1,
                }
            return self._fake_outcome(kwargs["job_id"])

        settings = self._settings()
        with patch(
            "capital_radar.extraction.build_async_client",
            side_effect=lambda s: _FakeClient(),
        ), patch(
            "capital_radar.extraction.run_extraction_v2_async",
            side_effect=_fake_extract,
        ):
            result = analyze_documents(
                self.store, settings, limit=10, dry_run=False
            )

        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["provider_error"], "provider timeout")


if __name__ == "__main__":
    unittest.main()
