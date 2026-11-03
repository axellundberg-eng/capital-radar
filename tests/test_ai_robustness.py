from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisCandidate, AnalysisStore, FetchCandidate
from capital_radar.db import Database
from capital_radar.document_quality import evaluate_document_quality
from capital_radar.extraction import analyze_documents
from capital_radar.manual_actions import ManualActionService
from capital_radar.models import (
    AttachmentCandidate,
    DocumentCandidate,
    SecurityCandidate,
)
from capital_radar.normalization import stable_hash


class RobustAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(
            Path(self.temporary_directory.name) / "robust-ai.db"
        )
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="ROBUST-1",
                issuer_name="Robust AB",
                display_name="Robust",
                symbol="ROB",
                isin="SE0000000101",
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
                    WHERE source_id='nasdaq_nordic'
                      AND external_id='ROBUST-1'
                    """
                ).fetchone()[0]
            )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _report_candidate(self) -> tuple[AnalysisStore, FetchCandidate]:
        self.db.upsert_document(
            DocumentCandidate(
                source_id="mfn",
                external_id="robust-q1",
                issuer_name="Robust AB",
                title="Robust AB publishes interim report Q1 2026",
                canonical_url="https://www.mfn.se/a/robust/q1",
                published_at="2026-05-01T07:00:00+02:00",
                attachments=(
                    AttachmentCandidate(
                        "https://storage.mfn.se/robust-press-release.pdf",
                        mime_type="application/pdf",
                    ),
                    AttachmentCandidate(
                        "https://storage.mfn.se/robust-full-report.pdf",
                        mime_type="application/pdf",
                    ),
                ),
            )
        )
        store = AnalysisStore(self.db)
        candidate = store.fetch_candidates(
            1, issuer_id=self.issuer_id, prefer_reports=True
        )[0]
        return store, candidate

    def test_full_report_beats_incomplete_press_release(self) -> None:
        store, candidate = self._report_candidate()
        targets = {target.url: target for target in candidate.urls}
        press_text = (
            "[PAGE 1]\nInterim report Q1 2026. "
            "Cash flow from operating activities was SEK -12 million."
        )
        full_text = (
            "[PAGE 1]\nInterim report Q1 2026.\n"
            "[PAGE 5]\nBalance sheet. Cash and cash equivalents "
            "were SEK 40 million.\n"
            "[PAGE 8]\nCash flow from operating activities "
            "was SEK -12 million.\n"
            "[PAGE 12]\nSubsequent events."
        )
        for url, text, pages in (
            (
                "https://storage.mfn.se/robust-press-release.pdf",
                press_text,
                4,
            ),
            (
                "https://storage.mfn.se/robust-full-report.pdf",
                full_text,
                12,
            ),
        ):
            quality = evaluate_document_quality(
                text, profile_category="report", page_count=pages
            )
            store.save_content(
                candidate=candidate,
                attachment_id=targets[url].attachment_id,
                source_url=url,
                final_url=url,
                local_path=None,
                mime_type="application/pdf",
                sha256=stable_hash(url),
                byte_size=len(text),
                extraction_method="fixture",
                extracted_text=text,
                text_sha256=stable_hash(text),
                page_count=pages,
                status="extracted",
                quality_score=quality.score,
                quality_status=quality.status,
                quality_reason=quality.reason,
            )
        readiness = store.document_readiness(candidate.document_id)
        self.assertEqual(readiness["readiness"], "complete")
        self.assertEqual(readiness["id"], candidate.document_id)
        self.assertIsInstance(readiness["content_id"], int)
        self.assertEqual(
            readiness["source_url"],
            "https://storage.mfn.se/robust-full-report.pdf",
        )
        analysis = store.analysis_candidates(
            1, issuer_id=self.issuer_id, prefer_reports=True
        )
        self.assertEqual(len(analysis), 1)
        self.assertEqual(
            analysis[0].source_url,
            "https://storage.mfn.se/robust-full-report.pdf",
        )

    def test_expired_job_is_reclaimed_atomically_and_clears_error(self) -> None:
        store, fetch_candidate = self._report_candidate()
        text = (
            "[PAGE 1]\nCash and cash equivalents SEK 10 million. "
            "Cash flow from operating activities SEK -2 million."
        )
        target = fetch_candidate.urls[0]
        quality = evaluate_document_quality(
            text, profile_category="report", page_count=8
        )
        content_id = store.save_content(
            candidate=fetch_candidate,
            attachment_id=target.attachment_id,
            source_url=target.url,
            final_url=target.url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash(target.url),
            byte_size=len(text),
            extraction_method="fixture",
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=8,
            status="extracted",
            quality_score=quality.score,
            quality_status=quality.status,
            quality_reason=quality.reason,
        )
        candidate = AnalysisCandidate(
            document_id=fetch_candidate.document_id,
            issuer_id=self.issuer_id,
            issuer_name="Robust AB",
            title=fetch_candidate.title,
            published_at=fetch_candidate.published_at,
            profile_category="report",
            language=None,
            content_id=content_id,
            source_url=target.url,
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=8,
        )
        job_id, status, old_lease = store.create_job(
            candidate=candidate,
            model="kimi-k3",
            prompt_version="test",
            input_sha256="same-input",
            selected_char_count=len(text),
            estimated_input_tokens=30,
            deadline_at="2000-01-01T00:00:00+00:00",
            provider="moonshot",
        )
        self.assertEqual(status, "created")
        self.assertIsNotNone(old_lease)
        reclaimed_id, reclaimed_status, new_lease = store.create_job(
            candidate=candidate,
            model="kimi-k3",
            prompt_version="test",
            input_sha256="same-input",
            selected_char_count=len(text),
            estimated_input_tokens=30,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="moonshot",
        )
        self.assertEqual(reclaimed_id, job_id)
        self.assertEqual(reclaimed_status, "reclaimed")
        self.assertNotEqual(new_lease, old_lease)
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT status, error, attempt_count, lease_token
                FROM extraction_jobs WHERE id=?
                """,
                (job_id,),
            ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["error"])
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual(row["lease_token"], new_lease)
        self.assertFalse(store.fail_job(job_id, str(old_lease), "late"))
        self.assertTrue(store.fail_job(job_id, str(new_lease), "current"))

    def test_duplicate_interpret_click_reuses_one_action(self) -> None:
        service = ManualActionService(
            self.db,
            SimpleNamespace(action_timeout=175.0),
        )
        first = service.enqueue_interpret(self.issuer_id)
        second = service.enqueue_interpret(self.issuer_id)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        with self.db.connect() as connection:
            active = connection.execute(
                """
                SELECT COUNT(*) FROM manual_action_runs
                WHERE issuer_id=? AND status IN ('queued', 'running')
                """,
                (self.issuer_id,),
            ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_interpret_actions_for_different_issuers_run_in_parallel(self) -> None:
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture-2")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="ROBUST-2",
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
            second_issuer_id = int(
                connection.execute(
                    """
                    SELECT issuer_id FROM securities
                    WHERE source_id='nasdaq_nordic'
                      AND external_id='ROBUST-2'
                    """
                ).fetchone()[0]
            )
        service = ManualActionService(
            self.db,
            SimpleNamespace(action_timeout=175.0),
        )
        first = service.enqueue_interpret(self.issuer_id)
        second = service.enqueue_interpret(second_issuer_id)
        # Sedan singleton-guard togs bort (MAP 16/bulkheads) ska två OLIKA
        # bolag kunna claimas och köras parallellt — det är hela poängen med
        # flera worker-trådar. Samma bolag skyddas fortfarande av det unika
        # partial-indexet på (issuer_id, action_type).
        first_claim = service.claim_action(int(first["id"]))
        second_claim = service.claim_action(int(second["id"]))
        self.assertIsNotNone(first_claim)
        self.assertIsNotNone(second_claim)
        # En redan claimad action kan inte claimas igen (atomisk lease).
        self.assertIsNone(service.claim_action(int(first["id"])))
        self.assertEqual(
            service.action_status(int(first["id"]))["status"], "running"
        )
        self.assertEqual(
            service.action_status(int(second["id"]))["status"], "running"
        )
        lease_token, _ = first_claim
        service._fail_claimed_action(
            int(first["id"]), lease_token, "failed", "fixture"
        )
        self.assertEqual(
            service.action_status(int(first["id"]))["status"], "failed"
        )

    def test_repair_normalizes_completed_job_with_stale_error(self) -> None:
        store, fetch_candidate = self._report_candidate()
        target = fetch_candidate.urls[0]
        text = (
            "Cash and cash equivalents SEK 10 million. "
            "Cash flow from operating activities SEK -2 million."
        )
        quality = evaluate_document_quality(
            text, profile_category="report", page_count=8
        )
        content_id = store.save_content(
            candidate=fetch_candidate,
            attachment_id=target.attachment_id,
            source_url=target.url,
            final_url=target.url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash(target.url),
            byte_size=len(text),
            extraction_method="fixture",
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=8,
            status="extracted",
            quality_score=quality.score,
            quality_status=quality.status,
            quality_reason=quality.reason,
        )
        candidate = AnalysisCandidate(
            document_id=fetch_candidate.document_id,
            issuer_id=self.issuer_id,
            issuer_name="Robust AB",
            title=fetch_candidate.title,
            published_at=fetch_candidate.published_at,
            profile_category="report",
            language=None,
            content_id=content_id,
            source_url=target.url,
            extracted_text="fixture",
            text_sha256="fixture",
            page_count=1,
        )
        job_id, _, _ = store.create_job(
            candidate=candidate,
            model="kimi-k3",
            prompt_version="repair",
            input_sha256="repair-input",
            selected_char_count=7,
            estimated_input_tokens=2,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="moonshot",
        )
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE extraction_jobs
                SET status='completed', error='stale', lease_token='old'
                WHERE id=?
                """,
                (job_id,),
            )
        repaired = store.repair_ai_state()
        self.assertEqual(repaired["completed_jobs_normalized"], 1)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT error, lease_token FROM extraction_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        self.assertIsNone(row["error"])
        self.assertIsNone(row["lease_token"])

    def test_completed_job_with_amountless_confirmed_cash_event_is_reclaimed(
        self,
    ) -> None:
        # Ett jobb vars financing_event beskriver bekräftat kassaflöde men
        # saknar belopp (och står i preliminärt status) ska inte låsas av
        # idempotency. Det är signaturen för ett utfall som extraherats med
        # äldre prompt: beskrivningen säger "genomfört" men status/belopp
        # hänger inte med. Jobbet ska återtas och köras om.
        store, fetch_candidate = self._report_candidate()
        text = (
            "[PAGE 1]\nRights issue outcome announced, oversubscribed. "
            "Total issue proceeds approximately SEK 73.6 million."
        )
        target = fetch_candidate.urls[0]
        quality = evaluate_document_quality(
            text, profile_category="financing", page_count=1
        )
        content_id = store.save_content(
            candidate=fetch_candidate,
            attachment_id=target.attachment_id,
            source_url=target.url,
            final_url=target.url,
            local_path=None,
            mime_type="application/pdf",
            sha256=stable_hash(target.url),
            byte_size=len(text),
            extraction_method="fixture",
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=1,
            status="extracted",
            quality_score=quality.score,
            quality_status=quality.status,
            quality_reason=quality.reason,
        )
        candidate = AnalysisCandidate(
            document_id=fetch_candidate.document_id,
            issuer_id=self.issuer_id,
            issuer_name="Robust AB",
            title=fetch_candidate.title,
            published_at=fetch_candidate.published_at,
            profile_category="financing",
            language=None,
            content_id=content_id,
            source_url=target.url,
            extracted_text=text,
            text_sha256=stable_hash(text),
            page_count=1,
        )
        job_id, status, lease = store.create_job(
            candidate=candidate,
            model="deepseek-flash",
            prompt_version="v2",
            input_sha256="same-input",
            selected_char_count=len(text),
            estimated_input_tokens=30,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="deepseek",
        )
        self.assertEqual(status, "created")
        parsed_output = {
            "report": {
                "is_financial_report": False,
                "period_start": None,
                "period_end": None,
                "publication_date": "2026-07-24",
                "currency": "SEK",
                "reported_scale": None,
                "period_months": None,
                "confidence": 0.0,
                "facts": [],
            },
            "quality": {
                "requires_human_review": True,
                "uncertainties": ["belopp saknas"],
            },
            "financing_events": [
                {
                    "event_type": "rights_issue",
                    "description": "Rights issue outcome announced, oversubscribed.",
                    "announcement_date": "2026-07-24",
                    "effective_date": None,
                    "status": "announced",
                    "cash_direction": "inflow",
                    "gross_amount": None,
                    "net_amount": None,
                    "cash_effect_amount": None,
                    "currency": "SEK",
                    "liquidity_effect_kind": "confirmed_cash",
                    "committed_amount": None,
                    "conditional_amount": None,
                    "reflected_in_report": "no",
                    "conditions": None,
                    "use_of_proceeds": None,
                    "confidence": 0.5,
                    "evidence_label": "utfall",
                    "source_page": 1,
                    "source_excerpt": "oversubscribed",
                }
            ],
        }
        completed = store.complete_job(
            job_id,
            str(lease),
            response_id="r1",
            finish_reason="stop",
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cached_tokens": 0,
                "total_tokens": 2,
            },
            raw_response={},
            parsed_output=parsed_output,
        )
        self.assertTrue(completed)
        # Samma input igen: jobbet ska INTE returneras som 'completed'
        # (idempotent), utan återtas för om-extraktion.
        reclaimed_id, reclaimed_status, _ = store.create_job(
            candidate=candidate,
            model="deepseek-flash",
            prompt_version="v2",
            input_sha256="same-input",
            selected_char_count=len(text),
            estimated_input_tokens=30,
            deadline_at="2099-01-01T00:00:00+00:00",
            provider="deepseek",
        )
        self.assertEqual(reclaimed_id, job_id)
        self.assertEqual(reclaimed_status, "reclaimed")


if __name__ == "__main__":
    unittest.main()
