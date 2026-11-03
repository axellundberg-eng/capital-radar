from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.normalization import utc_now
from capital_radar.runway import compute_runways


class RunwayTests(unittest.TestCase):
    def test_new_calculation_retains_historical_rows_and_analysis_references(self):
        from copy import deepcopy
        from capital_radar.runway import _save_assessment
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'history.db')
            db.initialize()
            self._seed(db)
            store = AnalysisStore(db)
            first = compute_runways(store)['assessments'][0]
            with db.connect() as c:
                old = dict(c.execute('SELECT * FROM runway_assessments').fetchone())
                issuer = old['issuer_id']
                c.execute("UPDATE analysis_versions SET runway_assessment_id=? WHERE issuer_id=?", (old['id'],issuer))
                c.execute("UPDATE issuer_analysis_summary SET current_runway_assessment_id=? WHERE issuer_id=?", (old['id'],issuer))
            newer = deepcopy(old)
            newer['calculation_sha256'] = 'a' * 64
            newer['as_of_date'] = '2030-01-01'
            self.assertTrue(_save_assessment(store,newer))
            self.assertFalse(_save_assessment(store,newer))
            with db.connect() as c:
                self.assertEqual(dict(c.execute('SELECT * FROM runway_assessments WHERE id=?',(old['id'],)).fetchone()), old)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM runway_assessments').fetchone()[0], 2)
                self.assertEqual(c.execute('SELECT runway_assessment_id FROM analysis_versions').fetchone()[0], old['id'])
                self.assertEqual(c.execute('SELECT current_runway_assessment_id FROM issuer_analysis_summary').fetchone()[0], old['id'])
                # Läsvägen via radar_analysis.current_analysis testas igen i fas 3.

    def test_completed_cash_and_committed_facility_adjust_scores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "runway.db")
            db.initialize()
            self._seed(db)
            result = compute_runways(AnalysisStore(db))
            self.assertEqual(result["created"], 1)
            assessment = result["assessments"][0]
            self.assertEqual(assessment["operating_runway_months"], 5.0)
            self.assertEqual(assessment["adjusted_runway_months"], 10.5)
            self.assertEqual(assessment["operating_need_score"], 85)
            self.assertEqual(assessment["adjusted_need_score"], 55)
            self.assertEqual(assessment["financing_adjustment_points"], -30)
            self.assertEqual(result["interpretations_created"], 1)
            with db.connect() as connection:
                interpretation = connection.execute(
                    """
                    SELECT headline, need_score, source_document_ids_json
                    FROM capital_need_interpretations
                    """
                ).fetchone()
            self.assertIsNotNone(interpretation)
            self.assertEqual(interpretation["need_score"], 55)
            self.assertIn("Operativ runway", interpretation["headline"])

    def test_prefers_older_complete_report_when_latest_lacks_facts(self) -> None:
        """En nyare rapport utan kassa/OCF far inte blockera runway.

        Speglar 4C-fallet: Q2-rapporten extraherades utan cash/OCF (t.ex.
        efter ett tidigare TEXT=NULL-fel), medan Q1-rapporten har kompletta
        siffror. Runway ska da baseras pa Q1, inte fastna pa Q2.
        """
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "runway.db")
            db.initialize()
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    """
                    INSERT INTO issuers (
                        canonical_name, normalized_name, identity_status,
                        created_at, updated_at
                    ) VALUES ('4C Group', '4c', 'provisional', ?, ?)
                    """,
                    (now, now),
                ).lastrowid

                def add_report(
                    title: str,
                    period_end: str,
                    published: str,
                    with_facts: bool,
                ) -> None:
                    document_id = connection.execute(
                        """
                        INSERT INTO documents (
                            issuer_id, event_key, canonical_url, title,
                            published_at, cash_relevance_score,
                            relevance_reason, queued_for_analysis,
                            analysis_status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 100, 'report', 1,
                                  'extracted', ?, ?)
                        """,
                        (
                            issuer_id,
                            f"key-{period_end}",
                            f"https://storage.mfn.se/{period_end}.pdf",
                            title,
                            published,
                            now,
                            now,
                        ),
                    ).lastrowid
                    content_id = connection.execute(
                        """
                        INSERT INTO document_contents (
                            document_id, source_url, extracted_text,
                            text_sha256, char_count, status, created_at,
                            updated_at
                        ) VALUES (?, ?, 'text', 'hash', 4, 'extracted', ?, ?)
                        """,
                        (
                            document_id,
                            f"https://storage.mfn.se/{period_end}.pdf",
                            now,
                            now,
                        ),
                    ).lastrowid
                    job_id = connection.execute(
                        """
                        INSERT INTO extraction_jobs (
                            issuer_id, document_id, content_id, provider,
                            model, prompt_version, input_sha256,
                            selected_char_count, estimated_input_tokens,
                            status, started_at, completed_at
                        ) VALUES (?, ?, ?, 'moonshot', 'fixture', 'v1',
                                  'input', 4, 1, 'completed', ?, ?)
                        """,
                        (issuer_id, document_id, content_id, now, now),
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
                        ) VALUES (?, ?, ?, 1, '2026-01-01', ?, ?, 'SEK',
                                  'millions', 6, 0.95, 0, '[]', ?, ?)
                        """,
                        (
                            issuer_id,
                            document_id,
                            job_id,
                            period_end,
                            published[:10],
                            now,
                            now,
                        ),
                    )
                    if with_facts:
                        for fact_type, value in (
                            ("cash_and_cash_equivalents", 435000.0),
                            ("operating_cash_flow", -28971000.0),
                        ):
                            connection.execute(
                                """
                                INSERT INTO financial_facts (
                                    issuer_id, document_id,
                                    extraction_job_id, fact_type,
                                    normalized_value, currency,
                                    evidence_label, source_page,
                                    source_excerpt, confidence, created_at
                                ) VALUES (?, ?, ?, ?, ?, 'SEK',
                                          'reported_fact', 4, 'ev', 0.95, ?)
                                """,
                                (
                                    issuer_id,
                                    document_id,
                                    job_id,
                                    fact_type,
                                    value,
                                    now,
                                ),
                            )

                # Nyare rapport (Q2) UTAN cash/OCF — ska inte valjas.
                add_report(
                    "Q2 2026", "2026-06-30", "2026-08-05T08:00:00+02:00",
                    with_facts=False,
                )
                # Aldre rapport (Q1) MED cash/OCF — ska valjas.
                add_report(
                    "Q1 2026", "2026-03-31", "2026-04-25T08:00:00+02:00",
                    with_facts=True,
                )

            result = compute_runways(AnalysisStore(db))
            self.assertEqual(result["created"], 1)
            with db.connect() as connection:
                row = connection.execute(
                    """
                    SELECT report_period_end, baseline_cash, operating_cash_flow
                    FROM runway_assessments
                    """
                ).fetchone()
            # Runway ska baseras pa Q1-rapporten (2026-03-31), inte Q2.
            self.assertEqual(row["report_period_end"], "2026-03-31")
            self.assertEqual(row["baseline_cash"], 435000.0)
            self.assertEqual(row["operating_cash_flow"], -28971000.0)

    def test_same_day_and_portfolio_company_financing_do_not_inflate_issuer_cash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "runway.db")
            db.initialize()
            self._seed(db)
            now = utc_now()
            with db.connect() as connection:
                issuer_id = connection.execute(
                    "SELECT id FROM issuers WHERE canonical_name='Medtech AB'"
                ).fetchone()[0]
                document_id = connection.execute(
                    """INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title, published_at,
                        cash_relevance_score, relevance_reason, queued_for_analysis,
                        analysis_status, created_at, updated_at
                    ) VALUES (?, 'portfolio-issue', 'https://mfn.se/portfolio',
                    'Medtech AB portföljbolag genomför riktad emission',
                    '2026-07-10T08:00:00+02:00', 100, 'eget kapital', 1,
                    'extracted', ?, ?)""",
                    (issuer_id, now, now),
                ).lastrowid
                job_id = connection.execute(
                    "SELECT id FROM extraction_jobs ORDER BY id LIMIT 1"
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO financing_events (
                        issuer_id, event_fingerprint, event_type, description,
                        effective_date, status, cash_direction, cash_effect_amount,
                        currency, liquidity_effect_kind, reflected_in_report,
                        confidence, created_at, updated_at
                    ) VALUES (?, 'same-day', 'rights_issue', 'Already in period',
                    '2026-06-30', 'completed', 'inflow', 30e6, 'SEK',
                    'confirmed_cash', 'no', .9, ?, ?)""",
                    (issuer_id, now, now),
                )
                portfolio_id = connection.execute(
                    """INSERT INTO financing_events (
                        issuer_id, event_fingerprint, event_type, description,
                        effective_date, status, cash_direction, cash_effect_amount,
                        currency, liquidity_effect_kind, reflected_in_report,
                        confidence, created_at, updated_at
                    ) VALUES (?, 'portfolio', 'directed_issue', 'Portfolio financing',
                    '2026-07-15', 'completed', 'inflow', 40e6, 'SEK',
                    'confirmed_cash', 'no', .9, ?, ?)""",
                    (issuer_id, now, now),
                ).lastrowid
                connection.execute(
                    """INSERT INTO financing_event_evidence (
                        financing_event_id, document_id, extraction_job_id,
                        evidence_label, source_excerpt, created_at
                    ) VALUES (?, ?, ?, 'reported_fact', 'Portfolio issue', ?)""",
                    (portfolio_id, document_id, job_id, now),
                )
            result = compute_runways(AnalysisStore(db))
            with db.connect() as connection:
                assessment = connection.execute(
                    """SELECT confirmed_post_report_adjustment, adjusted_runway_months
                    FROM runway_assessments"""
                ).fetchone()
            # The seed's genuine 20 MSEK post-report issue remains; neither
            # 30 MSEK on report date nor 40 MSEK in a portfolio company may count.
            self.assertEqual(assessment["confirmed_post_report_adjustment"], 20_000_000.0)
            # The existing 50 MSEK facility is weighted at 70%, so 70 + 35
            # MSEK gives 10.5 months. The excluded 70 MSEK never enters cash.
            self.assertEqual(assessment["adjusted_runway_months"], 10.5)

    @staticmethod
    def _seed(db: Database) -> None:
        now = utc_now()
        with db.connect() as connection:
            issuer_id = connection.execute(
                """
                INSERT INTO issuers (
                    canonical_name, normalized_name, identity_status,
                    created_at, updated_at
                ) VALUES ('Medtech AB', 'medtech', 'provisional', ?, ?)
                """,
                (now, now),
            ).lastrowid
            document_id = connection.execute(
                """
                INSERT INTO documents (
                    issuer_id, event_key, canonical_url, title, published_at,
                    cash_relevance_score, relevance_reason, queued_for_analysis,
                    analysis_status, created_at, updated_at
                ) VALUES (?, 'report-key', 'https://storage.mfn.se/report.pdf',
                          'Q2 report', '2026-07-20T08:00:00+02:00',
                          100, 'report', 1, 'extracted', ?, ?)
                """,
                (issuer_id, now, now),
            ).lastrowid
            content_id = connection.execute(
                """
                INSERT INTO document_contents (
                    document_id, source_url, extracted_text, text_sha256,
                    char_count, status, created_at, updated_at
                ) VALUES (?, 'https://storage.mfn.se/report.pdf', 'text',
                          'hash', 4, 'extracted', ?, ?)
                """,
                (document_id, now, now),
            ).lastrowid
            job_id = connection.execute(
                """
                INSERT INTO extraction_jobs (
                    issuer_id, document_id, content_id, provider, model,
                    prompt_version, input_sha256, selected_char_count,
                    estimated_input_tokens, status, started_at, completed_at
                ) VALUES (?, ?, ?, 'moonshot', 'fixture', 'v1', 'input',
                          4, 1, 'completed', ?, ?)
                """,
                (issuer_id, document_id, content_id, now, now),
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
                (issuer_id, document_id, job_id, now, now),
            )
            facts = (
                ("cash_and_cash_equivalents", 50_000_000.0),
                ("operating_cash_flow", -60_000_000.0),
            )
            for fact_type, value in facts:
                connection.execute(
                    """
                    INSERT INTO financial_facts (
                        issuer_id, document_id, extraction_job_id, fact_type,
                        normalized_value, currency, evidence_label,
                        source_page, source_excerpt, confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'SEK', 'reported_fact', 4,
                              'fixture evidence', 0.95, ?)
                    """,
                    (issuer_id, document_id, job_id, fact_type, value, now),
                )
            connection.execute(
                """
                INSERT INTO financing_events (
                    issuer_id, event_fingerprint, event_type, description,
                    announcement_date, effective_date, status, cash_direction,
                    cash_effect_amount, currency, liquidity_effect_kind,
                    reflected_in_report, confidence, created_at, updated_at
                ) VALUES (?, 'cash-event', 'directed_issue', 'Completed issue',
                          '2026-07-10', '2026-07-10', 'completed', 'inflow',
                          20e6, 'SEK', 'confirmed_cash', 'no', 0.95, ?, ?)
                """,
                (issuer_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO financing_events (
                    issuer_id, event_fingerprint, event_type, description,
                    announcement_date, effective_date, status, cash_direction,
                    currency, liquidity_effect_kind, committed_amount,
                    reflected_in_report, confidence, created_at, updated_at
                ) VALUES (?, 'facility-event', 'credit_facility', 'Facility',
                          '2026-07-12', '2026-07-12', 'approved', 'none',
                          'SEK', 'committed_liquidity', 50e6, 'no', 0.90, ?, ?)
                """,
                (issuer_id, now, now),
            )


if __name__ == "__main__":
    unittest.main()
