"""OncoZenge regression for attaching a stored terms document to a round."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from capital_radar.db import Database
from capital_radar.radar_analysis import current_analysis
from capital_radar.transaction_corrections import (
    apply_transaction_correction,
    approve_transaction_correction,
    create_transaction_correction,
    revert_transaction_correction,
    transaction_correction_detail,
)
from capital_radar.transaction_feed import get_transaction_detail, get_transaction_feed


class OncoZengeGroupingCorrectionTests(unittest.TestCase):
    issuer_id = 279
    transaction_id = "transaction:279:rights_issue:14855"
    round_key = "oncozenge-foretradesemission-2026"
    original_documents = [14855, 23463, 23567]
    terms_document = 14858

    def setUp(self) -> None:
        Path(".tmp-tests").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=".tmp-tests")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "oncozenge.db")
        self.db.initialize()
        now = "2026-09-22T20:00:00Z"
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO issuers(id,canonical_name,normalized_name,identity_status,created_at,updated_at)
                   VALUES(279,'OncoZenge','oncozenge','verified',?,?)""",
                (now, now),
            )
            fixtures = [
                (14855, "OncoZenge resolves on a rights issue of approximately SEK 33.3 million",
                 "2026-08-25T17:40:00Z", "The Rights Issue amounts to SEK 33.3 million before costs.",
                 "announced", "announced", 33_300_000, None, None, None),
                (14858, "OncoZenge resolves on a rights issue of approximately SEK 33.3 million and publishes preliminary financial information for the second quarter of 2026",
                 "2026-08-25T17:45:00Z",
                 "The Rights Issue has a subscription price of SEK 3.00 per share. The net proceeds of the Rights Issue are approximately SEK 27.8 million, after deduction of issue-related costs of approximately SEK 5.5 million. Subscription commitment (SEK)Subscription commitment (%)Guarantee commitment (SEK)Guarantee commitment (%) Total5,474,49016.421,203,06463.6. Shares may be subscribed for in full under the allocation principles. Stockholm Corporate Finance AB is acting as financial adviser. Vator Securities AB is serving as issuing agent.",
                 "conditional", "terms_published", 33_300_000, 27_800_000, None, None),
                (23463, "OncoZenge announces the preliminary outcome of the rights issue",
                 "2026-09-18T17:18:00Z", "The preliminary outcome of the Rights Issue amounts to SEK 30.7 million.",
                 "completed", "preliminary_outcome", 30_700_000, None, None, None),
                (23567, "OncoZenge announces the final outcome of the rights issue",
                 "2026-09-22T18:00:00Z",
                 "The Rights Issue closed. 8,894,465 shares, corresponding to approximately 80.0 percent of the Rights Issue, were subscribed for with the support of subscription rights. 1,333,583 shares, corresponding to approximately 12.0 percent of the Rights Issue, were subscribed for without the support of subscription rights. In total, 10,228,048 shares, corresponding to approximately 92.0 percent of the Rights Issue, have been allotted in the Rights Issue. The guarantee was not utilised.",
                 "completed", "outcome_published", 30_700_000, None, 0.0, 0.0),
            ]
            for document_id, title, published_at, body, status, stage, gross, net, utilized, utilized_pct in fixtures:
                url = f"https://www.mfn.se/a/oncozenge/{document_id}"
                connection.execute(
                    """INSERT INTO documents(id,issuer_id,event_key,canonical_url,title,published_at,
                              language,profile_category,relevance_reason,created_at,updated_at)
                       VALUES(?,279,?,?,?,?,?,'financing','fixture',?,?)""",
                    (document_id, f"onco-{document_id}", url, title, published_at, "en", now, now),
                )
                connection.execute(
                    """INSERT INTO document_sources(document_id,source_id,source_external_id,
                              discovered_url,source_published_at,first_seen_at,last_seen_at,metadata_json)
                       VALUES(?,'mfn',?,?,?,?,?,'{}')""",
                    (document_id, f"onco-{document_id}", url, published_at, now, now),
                )
                content_id = connection.execute(
                    """INSERT INTO document_contents(document_id,source_url,extracted_text,text_sha256,
                              char_count,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,'extracted',?,?)""",
                    (document_id, url, body, hashlib.sha256(body.encode()).hexdigest(), len(body), now, now),
                ).lastrowid
                job_id = connection.execute(
                    """INSERT INTO extraction_jobs(issuer_id,document_id,content_id,provider,model,
                              prompt_version,input_sha256,selected_char_count,estimated_input_tokens,
                              status,attempt_count,started_at,completed_at)
                       VALUES(279,?,?,'fixture','fixture','fixture',?,1,1,'completed',1,?,?)""",
                    (document_id, content_id, f"hash-{document_id}", now, now),
                ).lastrowid
                event = {
                    "issuer_id": 279, "event_fingerprint": f"event-{document_id}",
                    "event_type": "rights_issue", "description": "OncoZenge rights issue",
                    "announcement_date": published_at[:10], "effective_date": published_at[:10],
                    "status": status, "cash_direction": "in", "gross_amount": gross,
                    "net_amount": net,
                    "cash_effect_amount": gross if stage == "outcome_published" else None,
                    "currency": "SEK",
                    "liquidity_effect_kind": (
                        "confirmed_cash" if stage == "outcome_published" else "conditional_liquidity"
                    ),
                    "reflected_in_report": "no", "confidence": 0.95,
                    "created_at": now, "updated_at": now, "financing_type": "rights_issue",
                    "canonical_round_key": self.round_key if document_id != 14858 else None,
                    "lifecycle_stage": stage, "stage_effective_at": published_at[:10],
                    "issue_price": 3.0 if document_id in {14858, 23567} else None,
                    "subscription_commitment_amount": None,
                    "subscription_commitment_pct": None,
                    "guarantee_commitment_amount": None,
                    "guarantee_commitment_pct": None,
                    "guarantee_utilized_amount": utilized, "guarantee_utilized_pct": utilized_pct,
                    "transaction_cost": 5_500_000 if document_id == 14858 else None,
                    "shares_issued": 10_228_048 if document_id == 23567 else None,
                    "final_subscription_pct": 0.92 if document_id == 23567 else None,
                    "adviser": (
                        "Stockholm Corporate Finance AB" if document_id in {14858, 23567} else None
                    ),
                    "issuer_agent": (
                        "Vator Securities AB" if document_id in {14858, 23567} else None
                    ),
                }
                event_id = connection.execute(
                    "INSERT INTO financing_events(" + ",".join(event) + ") VALUES(" +
                    ",".join("?" for _ in event) + ")", tuple(event.values()),
                ).lastrowid
                connection.execute(
                    """INSERT INTO financing_event_evidence(financing_event_id,document_id,
                              extraction_job_id,evidence_label,source_excerpt,created_at)
                       VALUES(?,?,?,'explicit',?,?)""",
                    (event_id, document_id, job_id, body, now),
                )
                if document_id == self.terms_document:
                    connection.execute(
                        """INSERT INTO report_extractions(issuer_id,document_id,extraction_job_id,
                                  is_financial_report,confidence,requires_human_review,uncertainties_json,
                                  created_at,updated_at)
                           VALUES(279,?,?,1,0.9,0,'[]',?,?)""",
                        (document_id, job_id, now, now),
                    )
            assessment = {
                "issuer_id": 279, "as_of_date": "2026-09-22", "report_document_id": 14855,
                "report_period_end": "2026-06-30", "currency": "SEK", "baseline_cash": 700_000,
                "operating_cash_flow": -3_000_000, "period_months": 3,
                "monthly_operating_burn": 1_000_000, "confirmed_post_report_adjustment": 0,
                "committed_liquidity": 21_203_064, "conditional_liquidity": 33_300_000,
                "adjusted_cash": 700_000, "weighted_liquidity": 21_203_064,
                "operating_runway_months": 0.7, "adjusted_runway_months": 21.9,
                "operating_need_score": 90, "financing_adjustment_points": 0,
                "adjusted_need_score": 90, "need_timing": "0-3_months", "alert_level": "high",
                "evidence_posture": "fixture", "calculation_json": "{}",
                "calculation_sha256": "onco-fixture", "created_at": now,
            }
            connection.execute(
                "INSERT INTO runway_assessments(" + ",".join(assessment) + ") VALUES(" +
                ",".join("?" for _ in assessment) + ")", tuple(assessment.values()),
            )

    def test_attach_terms_document_projects_complete_round_and_reverts_exactly(self) -> None:
        with self.db.connect() as connection:
            before_feed = get_transaction_feed(connection)["items"]
            before_json = json.dumps(before_feed, ensure_ascii=False, sort_keys=True)
            before = get_transaction_detail(connection, self.transaction_id)
            self.assertEqual(before["source_document_ids"], self.original_documents)
            reported_storage_before = tuple(connection.execute(
                """SELECT typeof(operating_runway_months),quote(operating_runway_months),
                          calculation_json,calculation_sha256 FROM runway_assessments
                   WHERE issuer_id=279"""
            ).fetchone())

            proposal = create_transaction_correction(connection, {
                "correction_type": "assign_document_to_round",
                "transaction_id": self.transaction_id,
                "issuer_id": self.issuer_id,
                "source_transaction_document_ids": self.original_documents,
                "document_id": self.terms_document,
                "canonical_round_key": self.round_key,
                "reason": "Villkorsdokumentet 14858 hör till samma källbelagda emissionsrunda.",
            }, actor="review-agent")
            approve_transaction_correction(connection, proposal["id"], actor="human-reviewer")
            apply_transaction_correction(connection, proposal["id"], actor="human-reviewer")

            corrected = get_transaction_detail(connection, self.transaction_id)
            self.assertEqual(corrected["source_document_ids"], [14855, 14858, 23463, 23567])
            dossier = corrected["details"]["financing_dossier"]
            self.assertEqual(dossier["gross_amount"], 30_700_000)
            self.assertEqual(dossier["announced_gross_amount"], 33_300_000)
            self.assertEqual(dossier["actual_gross_amount"], 30_700_000)
            self.assertEqual(dossier["announced_net_amount"], 27_800_000)
            self.assertIsNone(dossier["actual_net_amount"])
            self.assertEqual(dossier["transaction_cost"], 5_500_000)
            self.assertEqual(dossier["issue_price"], 3.0)
            self.assertEqual(dossier["subscription_commitment_amount"], 5_474_490)
            self.assertAlmostEqual(dossier["subscription_commitment_pct"], 0.164)
            self.assertEqual(dossier["guarantee_commitment_amount"], 21_203_064)
            self.assertAlmostEqual(dossier["guarantee_commitment_pct"], 0.636)
            self.assertEqual(dossier["shares_issued"], 10_228_048)
            self.assertAlmostEqual(dossier["final_subscription_pct"], 0.92)
            self.assertAlmostEqual(dossier["subscription_with_rights_pct"], 0.80)
            self.assertAlmostEqual(dossier["subscription_without_rights_pct"], 0.12)
            self.assertEqual(dossier["guarantee_utilized_amount"], 0)
            self.assertEqual(dossier["guarantee_utilized_pct"], 0)
            self.assertEqual(dossier["adviser"], "Stockholm Corporate Finance AB")
            self.assertEqual(dossier["issuer_agent"], "Vator Securities AB")
            self.assertIn(
                {"name": "Stockholm Corporate Finance AB", "role": "Finansiell rådgivare"},
                corrected["institutions"],
            )
            self.assertIn(
                {"name": "Vator Securities AB", "role": "Emissionsinstitut"},
                corrected["institutions"],
            )
            reconciliation = dossier["round_reconciliation"]
            self.assertEqual(reconciliation["terms"]["announced_gross_amount"]["value"], 33_300_000)
            self.assertEqual(reconciliation["actual_outcome"]["gross_amount"]["value"], 30_700_000)
            self.assertIsNone(reconciliation["actual_outcome"]["net_amount"]["value"])

            readback = transaction_correction_detail(connection, proposal["id"])
            self.assertEqual(readback["additional_document_ids"], [14858])
            self.assertEqual(readback["effective_document_grouping"], [{
                "canonical_round_key": self.round_key,
                "document_ids": [14855, 14858, 23463, 23567],
            }])
            after_analysis = current_analysis(connection, [279])[0]
            self.assertEqual(after_analysis["reported_runway_months"], 0.7)
            self.assertEqual(after_analysis["pro_forma_cash_adjustment"], 30_700_000)
            self.assertEqual(len(after_analysis["events"]), 1)
            self.assertNotIn(21_203_064, [event["cash_effect"] for event in after_analysis["events"]])
            self.assertEqual(tuple(connection.execute(
                """SELECT typeof(operating_runway_months),quote(operating_runway_months),
                          calculation_json,calculation_sha256 FROM runway_assessments
                   WHERE issuer_id=279"""
            ).fetchone()), reported_storage_before)

            revert_transaction_correction(connection, proposal["id"], actor="human-reviewer")
            reverted_json = json.dumps(
                get_transaction_feed(connection)["items"], ensure_ascii=False, sort_keys=True
            )
            self.assertEqual(reverted_json, before_json)
            reverted_readback = transaction_correction_detail(connection, proposal["id"])
            self.assertEqual(
                reverted_readback["effective_document_grouping"],
                [{"transaction_id": self.transaction_id, "document_ids": self.original_documents}],
            )


if __name__ == "__main__":
    unittest.main()
