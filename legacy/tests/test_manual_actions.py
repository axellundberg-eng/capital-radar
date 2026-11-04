from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisCandidate, AnalysisStore
from capital_radar.db import Database
from capital_radar.manual_actions import ManualActionService
from capital_radar.models import DocumentCandidate, SecurityCandidate
from capital_radar.normalization import utc_now
from capital_radar.sources.mfn import MfnDiscoveryResult


class ManualActionStoreTests(unittest.TestCase):
    def test_mfn_refresh_stores_non_cash_news_under_validated_issuer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "all-news.db")
            db.initialize()
            run_id = db.start_run(
                "nasdaq_nordic", "universe", "fixture"
            )
            db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id="ALL-NEWS",
                    issuer_name="Alla Nyheter AB",
                    display_name="Alla Nyheter",
                    symbol="ALL",
                    isin="SE0000000199",
                    currency="SEK",
                    market="Nasdaq Stockholm",
                    segment="Small Cap",
                ),
                run_id,
            )
            db.finish_run(run_id, status="success")
            with db.connect() as connection:
                issuer_id = int(
                    connection.execute(
                        """
                        SELECT issuer_id FROM securities
                        WHERE external_id='ALL-NEWS'
                        """
                    ).fetchone()[0]
                )
            settings = SimpleNamespace(
                action_timeout=175.0,
                http_timeout=1.0,
                min_request_interval=0.0,
                user_agent="test",
            )
            service = ManualActionService(db, settings)
            source_item = DocumentCandidate(
                source_id="mfn",
                external_id="all-news-1",
                issuer_name="Source Legal Name plc",
                title="Bolaget tecknar avtal med ny kund",
                canonical_url="https://www.mfn.se/a/alla-nyheter/customer",
                published_at="2026-07-24T09:00:00+02:00",
            )
            result = MfnDiscoveryResult(
                items=(source_item,),
                response_hash="fixture",
                status=200,
                url="https://www.mfn.se/all/a/alla-nyheter",
            )
            with (
                patch.object(
                    service,
                    "_resolve_mfn_identity",
                    return_value=(
                        "alla-nyheter",
                        "https://www.mfn.se/all/a/alla-nyheter",
                    ),
                ),
                patch(
                    "capital_radar.manual_actions.MfnDiscoveryAdapter.fetch_issuer",
                    return_value=result,
                ),
            ):
                output = service.refresh_mfn(
                    issuer_id,
                    prepare_report=False,
                )
            self.assertEqual(output["seen"], 1)
            self.assertEqual(output["created"], 1)
            self.assertEqual(output["queued"], 0)
            with db.connect() as connection:
                document = connection.execute(
                    """
                    SELECT issuer_id, profile_category, queued_for_analysis
                    FROM documents
                    """
                ).fetchone()
                metadata = connection.execute(
                    """
                    SELECT metadata_json FROM document_sources
                    WHERE source_id='mfn'
                    """
                ).fetchone()[0]
            self.assertEqual(document["issuer_id"], issuer_id)
            self.assertEqual(document["profile_category"], "commercial_deal")
            self.assertEqual(document["queued_for_analysis"], 0)
            self.assertEqual(
                json.loads(metadata)["source_issuer_name"],
                "Source Legal Name plc",
            )

    def test_interpretation_is_idempotent_and_enables_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "manual-actions.db")
            db.initialize()
            run_id = db.start_run("nasdaq_nordic", "universe", "fixture")
            db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id="WATCH-1",
                    issuer_name="Bevakning AB",
                    display_name="Bevakning",
                    symbol="WATCH",
                    isin="SE0000000099",
                    currency="SEK",
                    market="Nasdaq Stockholm",
                    segment="Small Cap",
                ),
                run_id,
            )
            db.finish_run(run_id, status="success")
            with db.connect() as connection:
                issuer_id = int(
                    connection.execute(
                        """
                        SELECT issuer_id FROM securities
                        WHERE source_id='nasdaq_nordic' AND external_id='WATCH-1'
                        """
                    ).fetchone()[0]
                )
            item = {
                "issuer_id": issuer_id,
                "as_of_date": "2026-07-23",
                "method": "test",
                "need_level": "low",
                "need_score": 0,
                "need_timing": "no_signal",
                "headline": "Ingen signal",
                "summary": "Test",
                "capital_need": "Inget identifierat.",
                "transaction_context": None,
                "recommended_action": "Bevaka.",
                "cash_amount": None,
                "operating_cash_flow": None,
                "period_months": None,
                "currency": "SEK",
                "report_period_end": None,
                "confidence": 0.8,
                "evidence_posture": "test",
                "evidence_json": json.dumps([]),
                "source_document_ids_json": json.dumps([]),
                "interpretation_sha256": "fixed-hash",
            }
            store = AnalysisStore(db)
            self.assertTrue(store.save_interpretation(item))
            self.assertFalse(store.save_interpretation(item))
            self.assertEqual(db.ensure_active_universe_watchlist(), 1)
            store.set_watchlist(issuer_id, active=False)
            self.assertEqual(db.ensure_active_universe_watchlist(), 0)
            self.assertEqual(
                db.ensure_active_universe_watchlist(reactivate_existing=True),
                1,
            )
            db.upsert_document(
                DocumentCandidate(
                    source_id="mfn",
                    external_id="report-1",
                    issuer_name="Bevakning AB",
                    title="Bevakning publicerar kvartalsrapport Q2 2026",
                    canonical_url="https://www.mfn.se/a/bevakning/report",
                    published_at="2026-07-23T07:00:00+02:00",
                )
            )
            with db.connect() as connection:
                document_id = int(
                    connection.execute(
                        "SELECT id FROM documents WHERE issuer_id=?",
                        (issuer_id,),
                    ).fetchone()[0]
                )
                cursor = connection.execute(
                    """
                    INSERT INTO document_contents (
                        document_id, source_url, extracted_text, text_sha256,
                        char_count, page_count, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'extracted', ?, ?)
                    """,
                    (
                        document_id,
                        "https://www.mfn.se/a/bevakning/report",
                        "[PAGE 1]\nCash and cash equivalents 10",
                        "text-hash",
                        38,
                        1,
                        utc_now(),
                        utc_now(),
                    ),
                )
                content_id = int(cursor.lastrowid)
                self.assertEqual(
                    connection.execute(
                        "SELECT active FROM issuer_watchlist WHERE issuer_id=?",
                        (issuer_id,),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM capital_need_interpretations"
                    ).fetchone()[0],
                    1,
                )
            candidate = AnalysisCandidate(
                document_id=document_id,
                issuer_id=issuer_id,
                issuer_name="Bevakning",
                title="Bevakning publicerar kvartalsrapport Q2 2026",
                published_at="2026-07-23T07:00:00+02:00",
                profile_category="report",
                language="sv",
                content_id=content_id,
                source_url="https://www.mfn.se/a/bevakning/report",
                extracted_text="[PAGE 1]\nCash and cash equivalents 10",
                text_sha256="text-hash",
                page_count=1,
            )
            job_id, job_status, lease_token = store.create_job(
                candidate=candidate,
                model="kimi-k3",
                prompt_version="test-v1",
                input_sha256="input-hash",
                selected_char_count=38,
                estimated_input_tokens=13,
                deadline_at="2099-01-01T00:00:00+00:00",
                provider="moonshot",
            )
            self.assertEqual(job_status, "created")
            self.assertIsNotNone(lease_token)
            repeated_id, repeated_status, repeated_lease = store.create_job(
                candidate=candidate,
                model="kimi-k3",
                prompt_version="test-v1",
                input_sha256="input-hash",
                selected_char_count=38,
                estimated_input_tokens=13,
                deadline_at="2099-01-01T00:00:00+00:00",
                provider="moonshot",
            )
            self.assertEqual(repeated_id, job_id)
            self.assertEqual(repeated_status, "running")
            self.assertIsNone(repeated_lease)

    def test_fristaende_tolkning_hamtart_mfn_forst(self) -> None:
        # En fristående (icke-batch) tolkning ska alltid hämta nya
        # dokument från MFN innan analys. Verifierar att refresh_mfn
        # anropas även utan screening-batch.
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "refresh-first.db")
            db.initialize()
            run_id = db.start_run("nasdaq_nordic", "universe", "fixture")
            db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id="REF-1",
                    issuer_name="Refresh AB",
                    display_name="Refresh",
                    symbol="REF",
                    isin="SE0000000299",
                    currency="SEK",
                    market="Nasdaq Stockholm",
                    segment="Small Cap",
                ),
                run_id,
            )
            db.finish_run(run_id, status="success")
            with db.connect() as connection:
                issuer_id = int(
                    connection.execute(
                        "SELECT issuer_id FROM securities WHERE external_id='REF-1'"
                    ).fetchone()[0]
                )
            settings = SimpleNamespace(
                action_timeout=175.0,
                http_timeout=1.0,
                min_request_interval=0.0,
                user_agent="test",
            )
            service = ManualActionService(db, settings)
            calls: list[str] = []

            def fake_refresh(iss_id: int, **kwargs):
                calls.append("refresh")
                return {"issuer_id": iss_id, "seen": 0, "created": 0}

            # Inga dokument -> insufficient_facts, men refresh ska ha körts.
            with (
                patch.object(service, "refresh_mfn", side_effect=fake_refresh),
                patch.object(
                    service.store, "latest_report_readiness", return_value=None
                ),
            ):
                output, terminal = service._interpret_core(
                    issuer_id,
                    deadline_at="2099-01-01T00:00:00+00:00",
                    phase=lambda *a, **k: None,
                )
            self.assertEqual(calls, ["refresh"])
            self.assertEqual(terminal, "insufficient_facts")

    def test_batch_med_refresh_avstangd_hamtart_inte_mfn(self) -> None:
        # En screening-batch med refresh_mfn=False ska inte hämta MFN.
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "no-refresh.db")
            db.initialize()
            run_id = db.start_run("nasdaq_nordic", "universe", "fixture")
            db.upsert_security(
                SecurityCandidate(
                    source_id="nasdaq_nordic",
                    external_id="NOREF-1",
                    issuer_name="NoRefresh AB",
                    display_name="NoRefresh",
                    symbol="NOREF",
                    isin="SE0000000399",
                    currency="SEK",
                    market="Nasdaq Stockholm",
                    segment="Small Cap",
                ),
                run_id,
            )
            db.finish_run(run_id, status="success")
            with db.connect() as connection:
                issuer_id = int(
                    connection.execute(
                        "SELECT issuer_id FROM securities WHERE external_id='NOREF-1'"
                    ).fetchone()[0]
                )
            settings = SimpleNamespace(
                action_timeout=175.0,
                http_timeout=1.0,
                min_request_interval=0.0,
                user_agent="test",
            )
            service = ManualActionService(db, settings)
            calls: list[str] = []
            with (
                patch.object(
                    service,
                    "refresh_mfn",
                    side_effect=lambda *a, **k: calls.append("refresh"),
                ),
                patch.object(
                    service.store, "latest_report_readiness", return_value=None
                ),
            ):
                service._interpret_core(
                    issuer_id,
                    deadline_at="2099-01-01T00:00:00+00:00",
                    refresh_mfn_first=False,
                    phase=lambda *a, **k: None,
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
