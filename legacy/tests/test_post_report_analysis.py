from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.normalization import utc_now


class PostReportCandidatesTests(unittest.TestCase):
    def test_hamtar_bara_pm_efter_rapportperiodens_slut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            issuer_id, report_doc_id = self._seed(db)

            period_end = store.latest_report_period_end(issuer_id)
            self.assertEqual(period_end, "2026-06-30")

            candidates = store.post_report_candidates(
                issuer_id, period_end, exclude_document_id=report_doc_id
            )
            titles = [c.title for c in candidates]
            # Bara PM efter 2026-06-30, ej rapporten, ej gamla PM.
            self.assertIn("Riktad nyemission", titles)
            self.assertIn("Kreditfacilitet", titles)
            self.assertNotIn("Q2 report", titles)
            self.assertNotIn("Gammal nyhet", titles)
            # Äldst först.
            self.assertEqual(
                titles, sorted(titles, key=lambda t: titles.index(t))
            )

    def test_exkluderar_rapporten_sjalv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            issuer_id, report_doc_id = self._seed(db)
            period_end = store.latest_report_period_end(issuer_id)
            candidates = store.post_report_candidates(
                issuer_id, period_end, exclude_document_id=report_doc_id
            )
            self.assertNotIn(
                report_doc_id, [c.document_id for c in candidates]
            )

    def test_pm_utan_text_kraver_forberedelse_innan_urval(self) -> None:
        """Reproducerar produktionsbuggen för 4C Group / Cell Impact.

        Ett köat financing-PM publicerat efter rapportperioden men UTAN
        extraherad text (``document_contents`` saknas) syns inte i
        ``post_report_candidates`` — det måste först förberedas via
        ``fetch_candidates``/``prepare_documents`` så att texten extraheras.
        """
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            issuer_id, report_doc_id = self._seed(db)
            period_end = store.latest_report_period_end(issuer_id)
            now = utc_now()

            # Köat financing-PM efter rapportperioden UTAN extraherad text.
            with db.connect() as connection:
                pm_id = connection.execute(
                    """
                    INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title,
                        published_at, profile_category, cash_relevance_score,
                        relevance_reason, queued_for_analysis, analysis_status,
                        created_at, updated_at
                    ) VALUES (?, 'utfall', 'https://storage.mfn.se/utfall.pdf',
                              'Emissionen kraftigt övertecknad',
                              '2026-07-24T16:55:00+02:00', 'financing', 100,
                              'x', 1, 'metadata_only', ?, ?)
                    """,
                    (issuer_id, now, now),
                ).lastrowid

            # Utan text syns PM:et INTE i post-rapport-urvalet (buggen).
            before = store.post_report_candidates(
                issuer_id, period_end, exclude_document_id=report_doc_id
            )
            self.assertNotIn(pm_id, [c.document_id for c in before])

            # ... men det hittas av fetch_candidates (det prepare_documents
            # använder) eftersom texten saknas.
            fetch = store.fetch_candidates(25, issuer_id=issuer_id)
            self.assertIn(pm_id, [c.document_id for c in fetch])

            # Efter att texten extraherats (det prepare_documents gör) syns
            # PM:et i post-rapport-urvalet.
            with db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO document_contents (
                        document_id, source_url, extracted_text, text_sha256,
                        char_count, status, quality_status, created_at,
                        updated_at
                    ) VALUES (?, 'https://storage.mfn.se/utfall.pdf', 'text',
                              'hash', 4, 'extracted', 'complete', ?, ?)
                    """,
                    (pm_id, now, now),
                )
            after = store.post_report_candidates(
                issuer_id, period_end, exclude_document_id=report_doc_id
            )
            self.assertIn(pm_id, [c.document_id for c in after])

    @staticmethod
    def _seed(db: Database) -> tuple[int, int]:
        now = utc_now()
        with db.connect() as connection:
            issuer_id = connection.execute(
                """
                INSERT INTO issuers (
                    canonical_name, normalized_name, identity_status,
                    created_at, updated_at
                ) VALUES ('Testbolag AB', 'testbolag', 'provisional', ?, ?)
                """,
                (now, now),
            ).lastrowid

            def add_doc(
                title: str,
                published_at: str,
                category: str,
                event_key: str,
            ) -> int:
                doc_id = connection.execute(
                    """
                    INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title,
                        published_at, profile_category, cash_relevance_score,
                        relevance_reason, queued_for_analysis, analysis_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 100, 'x', 1, 'extracted', ?, ?)
                    """,
                    (
                        issuer_id,
                        event_key,
                        f"https://storage.mfn.se/{event_key}.pdf",
                        title,
                        published_at,
                        category,
                        now,
                        now,
                    ),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO document_contents (
                        document_id, source_url, extracted_text, text_sha256,
                        char_count, status, quality_status, created_at,
                        updated_at
                    ) VALUES (?, ?, 'text', 'hash', 4, 'extracted',
                              'complete', ?, ?)
                    """,
                    (
                        doc_id,
                        f"https://storage.mfn.se/{event_key}.pdf",
                        now,
                        now,
                    ),
                )
                return doc_id

            # Rapporten (period-slut 2026-06-30, publicerad 2026-07-20).
            report_doc_id = add_doc(
                "Q2 report", "2026-07-20T08:00:00+02:00", "report", "report"
            )
            # PM efter rapportperiodens slut — ska hämtas.
            add_doc(
                "Riktad nyemission",
                "2026-08-01T08:00:00+02:00",
                "financing",
                "emission",
            )
            add_doc(
                "Kreditfacilitet",
                "2026-07-25T08:00:00+02:00",
                "financing",
                "facility",
            )
            # Gammalt PM före rapportperiodens slut — ska INTE hämtas.
            add_doc(
                "Gammal nyhet",
                "2026-05-10T08:00:00+02:00",
                "financing",
                "old",
            )

            content_id = connection.execute(
                """
                SELECT id FROM document_contents WHERE document_id=?
                """,
                (report_doc_id,),
            ).fetchone()["id"]
            job_id = connection.execute(
                """
                INSERT INTO extraction_jobs (
                    issuer_id, document_id, content_id, provider, model,
                    prompt_version, input_sha256, selected_char_count,
                    estimated_input_tokens, status, started_at, completed_at
                ) VALUES (?, ?, ?, 'deepseek', 'fixture', 'v1', 'input',
                          4, 1, 'completed', ?, ?)
                """,
                (issuer_id, report_doc_id, content_id, now, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO report_extractions (
                    issuer_id, document_id, extraction_job_id,
                    is_financial_report, report_period_start,
                    report_period_end, publication_date, currency,
                    reported_scale, period_months, confidence,
                    requires_human_review, uncertainties_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 1, '2026-01-01', '2026-06-30',
                          '2026-07-20', 'SEK', 'millions', 6, 0.95, 0,
                          '[]', ?, ?)
                """,
                (issuer_id, report_doc_id, job_id, now, now),
            )
        return issuer_id, report_doc_id


class LatestReportPeriodEndFallbackTests(unittest.TestCase):
    """Reproducerar 4C Group-scenariot.

    Rapporten analyserades men AI-extractionen returnerade
    ``report_period_end: null`` i ``report_extractions`` (eller klassificerade
    inte dokumentet som finansiell rapport). Utan fallback skulle
    ``latest_report_period_end`` returnera None och hela post-rapport-passet
    skippas — emissions-PM:et extraheras aldrig och bolaget flaggas felaktigt.
    """

    def test_faller_tillbaka_pa_financial_facts_nar_extraction_saknar_period(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    """
                    INSERT INTO issuers (
                        canonical_name, normalized_name, identity_status,
                        created_at, updated_at
                    ) VALUES ('4C Group', '4c group', 'provisional', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
                doc_id = connection.execute(
                    """
                    INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title,
                        published_at, profile_category, cash_relevance_score,
                        relevance_reason, queued_for_analysis, analysis_status,
                        created_at, updated_at
                    ) VALUES (?, 'q2', 'https://storage.mfn.se/q2.pdf', 'Q2',
                              '2026-07-20T08:00:00+02:00', 'report', 100, 'x',
                              1, 'extracted', ?, ?)
                    """,
                    (issuer_id, now, now),
                ).lastrowid
                content_id = connection.execute(
                    """
                    INSERT INTO document_contents (
                        document_id, source_url, extracted_text, text_sha256,
                        char_count, status, quality_status, created_at,
                        updated_at
                    ) VALUES (?, 'https://storage.mfn.se/q2.pdf', 'text',
                              'hash', 4, 'extracted', 'complete', ?, ?)
                    """,
                    (doc_id, now, now),
                ).lastrowid
                job_id = connection.execute(
                    """
                    INSERT INTO extraction_jobs (
                        issuer_id, document_id, content_id, provider, model,
                        prompt_version, input_sha256, selected_char_count,
                        estimated_input_tokens, status, started_at, completed_at
                    ) VALUES (?, ?, ?, 'deepseek', 'fixture', 'v1', 'input',
                              4, 1, 'completed', ?, ?)
                    """,
                    (issuer_id, doc_id, content_id, now, now),
                ).lastrowid
                # Extractionen returnerade period_end NULL (buggen hos 4C).
                connection.execute(
                    """
                    INSERT INTO report_extractions (
                        issuer_id, document_id, extraction_job_id,
                        is_financial_report, report_period_start,
                        report_period_end, publication_date, currency,
                        reported_scale, period_months, confidence,
                        requires_human_review, uncertainties_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, NULL, NULL, '2026-07-20', 'SEK',
                              'millions', 6, 0.95, 0, '[]', ?, ?)
                    """,
                    (issuer_id, doc_id, job_id, now, now),
                )
                # ... men financial_facts har period_end (satts vid analysen).
                connection.execute(
                    """
                    INSERT INTO financial_facts (
                        issuer_id, document_id, extraction_job_id, fact_type,
                        reported_value, normalized_value, currency,
                        reported_unit, period_start, period_end, as_of_date,
                        evidence_label, source_page, source_excerpt,
                        confidence, created_at
                    ) VALUES (?, ?, ?, 'cash', 10.0, 10.0, 'SEK', 'millions',
                              '2026-01-01', '2026-06-30', '2026-06-30',
                              'Kassa', 1, 'x', 0.9, ?)
                    """,
                    (issuer_id, doc_id, job_id, now),
                )

            # Utan fallback hade detta varit None och post-rapport-passet
            # skippats. Nu ska perioden härledas från financial_facts.
            self.assertEqual(
                store.latest_report_period_end(issuer_id), "2026-06-30"
            )

    def test_returnerar_none_nar_varken_extraction_eller_facts_finns(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    """
                    INSERT INTO issuers (
                        canonical_name, normalized_name, identity_status,
                        created_at, updated_at
                    ) VALUES ('Tomt AB', 'tomt', 'provisional', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
            self.assertIsNone(store.latest_report_period_end(issuer_id))


class LatestReportReadinessTests(unittest.TestCase):
    """En nyare men ofullstandig rapport far inte blockera en komplett."""

    def test_valjer_senaste_kompletta_rapporten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    """
                    INSERT INTO issuers (
                        canonical_name, normalized_name, identity_status,
                        created_at, updated_at
                    ) VALUES ('Testbolag AB', 'testbolag', 'provisional', ?, ?)
                    """,
                    (now, now),
                ).lastrowid

                def add_report(
                    title: str,
                    published_at: str,
                    quality_status: str,
                ) -> int:
                    doc_id = connection.execute(
                        """
                        INSERT INTO documents (
                            issuer_id, event_key, canonical_url, title,
                            published_at, profile_category,
                            cash_relevance_score, relevance_reason,
                            queued_for_analysis, analysis_status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'report', 100, 'x', 1,
                                  'extracted', ?, ?)
                        """,
                        (
                            issuer_id,
                            title,
                            f"https://storage.mfn.se/{title}.pdf",
                            title,
                            published_at,
                            now,
                            now,
                        ),
                    ).lastrowid
                    connection.execute(
                        """
                        INSERT INTO document_contents (
                            document_id, source_url, extracted_text,
                            text_sha256, char_count, status, quality_status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'text', 'hash', 4, 'extracted', ?, ?, ?)
                        """,
                        (
                            doc_id,
                            f"https://storage.mfn.se/{title}.pdf",
                            quality_status,
                            now,
                            now,
                        ),
                    )
                    return doc_id

                # Aldre men komplett rapport (Q2).
                complete_id = add_report(
                    "Q2", "2026-07-20T08:00:00+02:00", "complete"
                )
                # Nyare men ofullstandig rapport (Q4 year-end, ej godkand).
                add_report("Q4", "2026-08-20T08:00:00+02:00", "insufficient")

            readiness = store.latest_report_readiness(issuer_id)
            # Ska valja den kompletta Q2-rapporten, inte den nyare trasiga Q4.
            self.assertIsNotNone(readiness)
            self.assertEqual(readiness["id"], complete_id)
            self.assertEqual(readiness["readiness"], "complete")

    def test_faller_tillbaka_pa_senaste_om_ingen_ar_komplett(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "t.db")
            db.initialize()
            store = AnalysisStore(db)
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    """
                    INSERT INTO issuers (
                        canonical_name, normalized_name, identity_status,
                        created_at, updated_at
                    ) VALUES ('Testbolag AB', 'testbolag', 'provisional', ?, ?)
                    """,
                    (now, now),
                ).lastrowid
                doc_id = connection.execute(
                    """
                    INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title,
                        published_at, profile_category, cash_relevance_score,
                        relevance_reason, queued_for_analysis, analysis_status,
                        created_at, updated_at
                    ) VALUES (?, 'q4', 'https://storage.mfn.se/q4.pdf', 'Q4',
                              '2026-08-20T08:00:00+02:00', 'report', 100, 'x',
                              1, 'extracted', ?, ?)
                    """,
                    (issuer_id, now, now),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO document_contents (
                        document_id, source_url, extracted_text, text_sha256,
                        char_count, status, quality_status, created_at,
                        updated_at
                    ) VALUES (?, 'https://storage.mfn.se/q4.pdf', 'text',
                              'hash', 4, 'extracted', 'insufficient', ?, ?)
                    """,
                    (doc_id, now, now),
                )

            readiness = store.latest_report_readiness(issuer_id)
            # Ingen komplett rapport: returnera senaste med sin status.
            self.assertIsNotNone(readiness)
            self.assertEqual(readiness["id"], doc_id)
            self.assertEqual(readiness["readiness"], "insufficient_facts")


if __name__ == "__main__":
    unittest.main()
