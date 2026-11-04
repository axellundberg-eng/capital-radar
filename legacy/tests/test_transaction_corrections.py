from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from capital_radar.db import Database
from capital_radar.radar_analysis import calculate
from capital_radar.transaction_corrections import (
    apply_transaction_correction,
    approve_transaction_correction,
    create_transaction_correction,
    grouping_projection_readback,
    rebuild_transaction,
    revert_transaction_correction,
    transaction_correction_detail,
)
from capital_radar.transaction_feed import get_transaction_feed


OLD_DOCUMENTS = [1406, 1407, 1410, 1411, 1412, 1413, 1414, 1415]
NEW_DOCUMENTS = [23040, 23041, 23355, 23356, 23538, 23539]
ALL_DOCUMENTS = OLD_DOCUMENTS + NEW_DOCUMENTS


class GroupingProjectionAcceptanceTests(unittest.TestCase):
    def test_known_fragmented_job_shapes_fail_and_exact_rounds_pass(self) -> None:
        cases = [
            ("precise-to2-2026", [23048, 23049, 24082, 24083],
             [[23048], [23049], [24082], [24083]]),
            ("freemelt-to1-exercise-2026-06",
             [7809, 7810, 7813, 7814, 7815, 7816, 7817, 7818],
             [[7809, 7810], [7813], [7814], [7815], [7816], [7817, 7818]]),
        ]
        for key, documents, fragmented in cases:
            with self.subTest(key=key):
                expected = [{"canonical_round_key": key, "document_ids": documents}]
                def feed(groups):
                    return [{"source_document_ids": docs, "details": {
                        "financing_dossier": {"canonical_round_key": key},
                    }} for docs in groups]
                self.assertFalse(grouping_projection_readback(
                    expected, feed(fragmented),
                )["effective_matches_applied"])
                self.assertTrue(grouping_projection_readback(
                    expected, feed([list(reversed(documents))]),
                )["effective_matches_applied"])


class TransactionGroupingCorrectionTests(unittest.TestCase):
    def _apply_initial_split(self, connection):
        initial = create_transaction_correction(connection, self.payload(), actor='review-agent')
        approve_transaction_correction(connection, initial['id'], actor='review-agent')
        apply_transaction_correction(connection, initial['id'], actor='review-agent')
        return initial['id']

    @staticmethod
    def regroup_payload():
        return {'correction_type': 'rebuild_transaction', 'transaction_id': 'transaction:117:rights_issue:23040',
                'issuer_id': 117, 'source_transaction_document_ids': NEW_DOCUMENTS,
                'resulting_rounds': [{'canonical_round_key': 'alzinova-final-rights-round-2026', 'document_ids': NEW_DOCUMENTS}],
                'reason': 'Hela den källverifierade senare unitrundan omgrupperas, utan att röra Q1-rundan.'}

    def test_full_reviewed_round_rebuild_preserves_originals_and_reverts_prior_ownership(self):
        from capital_radar.transaction_corrections import effective_document_rounds, TransactionCorrectionError
        with self.db.connect() as connection:
            original = [tuple(r) for r in connection.execute('SELECT * FROM financing_events ORDER BY id')]
            initial_id = self._apply_initial_split(connection)
            previous = effective_document_rounds(connection, ALL_DOCUMENTS)
            correction = create_transaction_correction(connection, self.regroup_payload(), actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            apply_transaction_correction(connection, correction['id'], actor='review-agent')
            current = effective_document_rounds(connection, ALL_DOCUMENTS)
            self.assertTrue(all(current[d] == 'alzinova-final-rights-round-2026' for d in NEW_DOCUMENTS))
            self.assertTrue(all(current[d] == previous[d] for d in OLD_DOCUMENTS))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_transaction_grouping_replacements').fetchone()[0], 6)
            self.assertTrue(transaction_correction_detail(connection, correction['id'])['grouping_matches_applied'])
            with self.assertRaises(TransactionCorrectionError):
                revert_transaction_correction(connection, initial_id, actor='review-agent')
            revert_transaction_correction(connection, correction['id'], actor='review-agent')
            self.assertEqual(effective_document_rounds(connection, ALL_DOCUMENTS), previous)
            self.assertEqual([tuple(r) for r in connection.execute('SELECT * FROM financing_events ORDER BY id')], original)
            revert_transaction_correction(connection, initial_id, actor='review-agent')

    def test_rebuild_readback_mismatch_rolls_back_replacement_before_recalculation(self):
        from capital_radar.transaction_corrections import effective_document_rounds, _affected_feed_items, GroupingReadbackMismatch
        with self.db.connect() as connection:
            self._apply_initial_split(connection)
            previous = effective_document_rounds(connection, ALL_DOCUMENTS)
            correction = create_transaction_correction(connection, self.regroup_payload(), actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            calls = 0
            def duplicate_rebuilt(*args, **kwargs):
                nonlocal calls
                cards = _affected_feed_items(*args, **kwargs)
                calls += 1
                return [*cards, dict(cards[0])] if calls == 2 else cards
            with patch('capital_radar.transaction_corrections._affected_feed_items', side_effect=duplicate_rebuilt), \
                    patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
                with self.assertRaises(GroupingReadbackMismatch):
                    apply_transaction_correction(connection, correction['id'], actor='review-agent')
                recalculate.assert_not_called()
            self.assertEqual(effective_document_rounds(connection, ALL_DOCUMENTS), previous)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_transaction_grouping_replacements').fetchone()[0], 0)

    def test_rebuild_cannot_move_only_part_of_a_previously_reviewed_round(self):
        from capital_radar.transaction_corrections import effective_document_rounds, TransactionCorrectionError
        with self.db.connect() as connection:
            self._apply_initial_split(connection)
            previous = effective_document_rounds(connection, ALL_DOCUMENTS)
            payload = {'correction_type': 'rebuild_transaction', 'transaction_id': 'transaction:117:rights_issue:1406',
                       'issuer_id': 117, 'source_transaction_document_ids': OLD_DOCUMENTS,
                       'additional_document_ids': [23040], 'resulting_rounds': [
                           {'canonical_round_key': previous[1406], 'document_ids': OLD_DOCUMENTS},
                           {'canonical_round_key': 'new-unverified-part', 'document_ids': [23040]}],
                       'reason': 'Incomplete old reviewed round must be rejected.'}
            correction = create_transaction_correction(connection, payload, actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            with self.assertRaisesRegex(TransactionCorrectionError, 'incomplete_previous_round'):
                apply_transaction_correction(connection, correction['id'], actor='review-agent')
            self.assertEqual(effective_document_rounds(connection, ALL_DOCUMENTS), previous)

    def test_rebuild_rejects_changed_actual_card_inventory(self):
        from capital_radar.transaction_corrections import effective_document_rounds, _affected_feed_items, TransactionCorrectionError
        with self.db.connect() as connection:
            self._apply_initial_split(connection)
            previous = effective_document_rounds(connection, ALL_DOCUMENTS)
            correction = create_transaction_correction(connection, self.regroup_payload(), actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            cards = _affected_feed_items(connection, NEW_DOCUMENTS, issuer_id=117)
            with patch('capital_radar.transaction_corrections._affected_feed_items', return_value=[*cards, dict(cards[0])]):
                with self.assertRaisesRegex(TransactionCorrectionError, 'expected_state_conflict'):
                    apply_transaction_correction(connection, correction['id'], actor='review-agent')
            self.assertEqual(effective_document_rounds(connection, ALL_DOCUMENTS), previous)

    def test_readback_checks_actual_feed_and_rejects_duplicate_round_rows(self) -> None:
        with self.db.connect() as connection:
            proposal = create_transaction_correction(connection, self.payload(), actor="agent")
            approve_transaction_correction(connection, proposal["id"], actor="agent")
            apply_transaction_correction(connection, proposal["id"], actor="agent")
            readback = transaction_correction_detail(connection, proposal["id"])
            self.assertIs(readback["grouping_matches_applied"], True)
            self.assertIs(readback["current_round_matches_applied"], True)
            feed = readback["effective_feed_current"]
            with patch("capital_radar.transaction_corrections._affected_feed_items", return_value=[*feed, dict(feed[0])]):
                stale = transaction_correction_detail(connection, proposal["id"])
            self.assertIs(stale["grouping_matches_applied"], False)
            self.assertIs(stale["current_round_matches_applied"], False)
            self.assertEqual(len(stale["effective_document_grouping"]), 3)

    def setUp(self) -> None:
        root = Path(".tmp-tests")
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "transaction-corrections.db")
        self.db.initialize()
        self._seed_alzinova()

    def _seed_alzinova(self) -> None:
        now = "2026-09-22T10:00:00Z"
        rows = [
            (1406, "Utfall i Alzinovas företrädesemission", "2026-02-25T08:00:00Z", "Företrädesemissionens preliminära utfall uppgår till 48 MSEK."),
            (1407, "Outcome in Alzinova's rights issue", "2026-02-25T08:01:00Z", "The preliminary outcome of the rights issue amounts to SEK 48 million."),
            (1410, "Slutligt utfall i Alzinovas företrädesemission", "2026-03-02T08:00:00Z", "Det slutliga utfallet i företrädesemissionen uppgår till 48 MSEK."),
            (1411, "Final outcome in Alzinova's rights issue", "2026-03-02T08:01:00Z", "The final outcome of the rights issue amounts to SEK 48 million."),
            (1412, "Alzinova genomför garantiemission efter företrädesemission", "2026-03-10T08:00:00Z", "Garantiemission efter avslutad företrädesemission."),
            (1413, "Alzinova carries out guarantee issue following rights issue", "2026-03-10T08:01:00Z", "Guarantee issue following the completed rights issue."),
            (1414, "Registrering av Alzinovas företrädesemission", "2026-03-18T08:00:00Z", "Företrädesemissionen har registrerats."),
            (1415, "Registration of Alzinova's rights issue", "2026-03-18T08:01:00Z", "The rights issue has been registered."),
            (23040, "Alzinova föreslår företrädesemission av units om cirka 51 MSEK", "2026-08-24T08:00:00Z", "Bolaget föreslår en företrädesemission av units om cirka 51 MSEK."),
            (23041, "Alzinova proposes rights issue of units of approximately SEK 51 million", "2026-08-24T08:01:00Z", "The company proposes a rights issue of units of approximately SEK 51 million."),
            (23355, "Villkor för Alzinovas företrädesemission om 51 MSEK", "2026-09-10T08:00:00Z", "Villkor för företrädesemissionen om 51 MSEK. TO5 och TO6 kan utnyttjas senare."),
            (23356, "Terms for Alzinova's rights issue of SEK 51 million", "2026-09-10T08:01:00Z", "Terms for the rights issue of SEK 51 million. TO5 and TO6 may be exercised later."),
            (23538, "Teckningsperioden i Alzinovas företrädesemission inleds idag", "2026-09-22T06:30:00Z", "Teckningsperioden löper 22 september–6 oktober 2026. Emissionen kan tillföra cirka 51 MSEK före kostnader."),
            (23539, "Subscription period in Alzinova's rights issue begins today", "2026-09-22T06:31:00Z", "The subscription period runs from 22 September to 6 October 2026. Gross proceeds may amount to SEK 51 million."),
        ]
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO issuers(id,canonical_name,normalized_name,identity_status,created_at,updated_at)
                   VALUES(117,'Alzinova AB','alzinova','verified',?,?)""",
                (now, now),
            )
            for document_id, title, published_at, body in rows:
                language = "en" if document_id % 2 else "sv"
                url = f"https://mfn.se/a/alzinova/{document_id}"
                connection.execute(
                    """INSERT INTO documents(
                           id,issuer_id,event_key,canonical_url,title,published_at,language,
                           profile_category,relevance_reason,created_at,updated_at
                       ) VALUES(?,117,?,?,?,?,?,'financing','fixture',?,?)""",
                    (document_id, f"fixture-{document_id}", url, title, published_at, language, now, now),
                )
                connection.execute(
                    """INSERT INTO document_sources(
                           document_id,source_id,source_external_id,discovered_url,
                           source_published_at,first_seen_at,last_seen_at,metadata_json
                       ) VALUES(?,'mfn',?,?,?,?,?,'{}')""",
                    (document_id, f"alzinova-{document_id}", url, published_at, now, now),
                )
                content_id = connection.execute(
                    """INSERT INTO document_contents(
                           document_id,source_url,extracted_text,text_sha256,char_count,
                           status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,'extracted',?,?)""",
                    (document_id, url, body, hashlib.sha256(body.encode()).hexdigest(), len(body), now, now),
                ).lastrowid
                job_id = connection.execute(
                    """INSERT INTO extraction_jobs(
                           issuer_id,document_id,content_id,provider,model,prompt_version,
                           input_sha256,selected_char_count,estimated_input_tokens,status,
                           attempt_count,started_at,completed_at
                       ) VALUES(117,?,?, 'test','test','test',?,1,1,'completed',1,?,?)""",
                    (document_id, content_id, f"hash-{document_id}", now, now),
                ).lastrowid
                old = document_id in OLD_DOCUMENTS
                stage = (
                    "outcome_published" if document_id in {1406, 1407, 1410, 1411}
                    else "registered" if document_id in {1414, 1415}
                    else "subscription_open" if document_id in {23538, 23539}
                    else "terms_published"
                )
                event_id = connection.execute(
                    """INSERT INTO financing_events(
                           issuer_id,event_fingerprint,event_type,description,announcement_date,
                           effective_date,status,cash_direction,gross_amount,currency,
                           liquidity_effect_kind,reflected_in_report,confidence,created_at,updated_at,
                           financing_type,canonical_round_key,lifecycle_stage,stage_effective_at
                       ) VALUES(117,?,'share_issue','Företrädesemission',?,?,?,'in',?,'SEK',
                                ?,'no',0.95,?,?,'rights_issue','incorrect_merged_round',?,?)""",
                    (
                        f"event-{document_id}", published_at[:10], published_at[:10],
                        "completed" if old else "announced",
                        48_000_000 if old else 51_000_000,
                        "confirmed_cash" if old else "conditional_liquidity",
                        now, now, stage, published_at[:10],
                    ),
                ).lastrowid
                connection.execute(
                    """INSERT INTO financing_event_evidence(
                           financing_event_id,document_id,extraction_job_id,evidence_label,
                           source_excerpt,created_at
                       ) VALUES(?,?,?,'explicit',?,?)""",
                    (event_id, document_id, job_id, body, now),
                )

    @staticmethod
    def payload() -> dict:
        return {
            "correction_type": "split_transaction",
            "transaction_id": "transaction:117:rights_issue:1406",
            "issuer_id": 117,
            "source_transaction_document_ids": ALL_DOCUMENTS,
            "resulting_rounds": [
                {"canonical_round_key": "alzinova_foretradesemission_2026_q1", "document_ids": OLD_DOCUMENTS},
                {"canonical_round_key": "alzinova_foretradesemission_units_2026", "document_ids": NEW_DOCUMENTS},
            ],
            "reason": "Dokument från två separata företrädesemissioner har grupperats som en transaktion.",
        }

    def test_exact_alzinova_split_and_revert(self) -> None:
        with self.db.connect() as connection:
            before = get_transaction_feed(connection)["items"]
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0]["id"], "transaction:117:rights_issue:1406")
            self.assertEqual(before[0]["source_document_ids"], ALL_DOCUMENTS)
            original_events = connection.execute(
                "SELECT canonical_round_key FROM financing_events ORDER BY id"
            ).fetchall()

            proposal = create_transaction_correction(connection, self.payload(), actor="review-agent")
            self.assertEqual(proposal["status"], "proposed")
            approve_transaction_correction(connection, proposal["id"], actor="human-reviewer")
            apply_transaction_correction(connection, proposal["id"], actor="human-reviewer")

            after = get_transaction_feed(connection)["items"]
            self.assertEqual(len(after), 2)
            by_documents = {tuple(item["source_document_ids"]): item for item in after}
            old = by_documents[tuple(OLD_DOCUMENTS)]
            new = by_documents[tuple(NEW_DOCUMENTS)]
            self.assertIn(old["status"], {"completed", "registered"})
            self.assertEqual(new["status"], "subscription_period")
            self.assertEqual(new["status_label"], "Teckningsperiod")
            self.assertEqual(new["published_at"], "2026-09-22T06:31:00Z")
            self.assertEqual(new["details"]["announced_amount"], 51_000_000)
            self.assertEqual(new["details"]["confirmed_cash_effect_amount"], 0.0)
            dossier = new["details"]["financing_dossier"]
            self.assertEqual(dossier["canonical_round_key"], "alzinova_foretradesemission_units_2026")
            self.assertNotIn("outcome", new["headline"].casefold())
            rebuilt = rebuild_transaction(
                connection, "transaction:117:rights_issue:1406"
            )
            self.assertEqual(len(rebuilt["items"]), 2)
            analysis = calculate(
                {
                    "issuer_id": 117,
                    "baseline_cash": 10_000_000,
                    "monthly_operating_burn": 5_000_000,
                    "operating_cash_flow": -5_000_000,
                    "operating_runway_months": 2.0,
                    "report_period_end": "2026-08-01",
                    "currency": "SEK",
                },
                after,
                {},
                as_of="2026-09-22",
            )
            self.assertEqual(analysis["reported_runway_months"], 2.0)
            # Annonserat utfall räknas inte som kassa utan manuell vikt; beloppet följer med.
            self.assertEqual(analysis["events"][0]["amount"], 51_000_000)
            self.assertEqual(len(analysis["events"]), 1)
            warrant_terms = connection.execute(
                """SELECT e.liquidity_effect_kind
                   FROM financing_events e
                   JOIN financing_event_evidence v ON v.financing_event_id=e.id
                   WHERE v.document_id IN (23355,23356)"""
            ).fetchall()
            self.assertEqual(
                {row["liquidity_effect_kind"] for row in warrant_terms},
                {"conditional_liquidity"},
            )

            readback = transaction_correction_detail(connection, proposal["id"])
            self.assertEqual(readback["applied_correction_ids"], [proposal["id"]])
            self.assertEqual(len(readback["feed_after_correction"]), 2)
            self.assertEqual(readback["feed_before_correction"]["source_document_ids"], ALL_DOCUMENTS)
            self.assertEqual(
                [row["canonical_round_key"] for row in connection.execute(
                    "SELECT canonical_round_key FROM financing_events ORDER BY id"
                )],
                [row["canonical_round_key"] for row in original_events],
            )

            revert_transaction_correction(connection, proposal["id"], actor="human-reviewer")
            reverted = get_transaction_feed(connection)["items"]
            self.assertEqual(len(reverted), 1)
            self.assertEqual(reverted[0]["source_document_ids"], ALL_DOCUMENTS)
            self.assertEqual(
                revert_transaction_correction(connection, proposal["id"], actor="human-reviewer")["status"],
                "reverted",
            )

    def test_grouping_apply_and_revert_recalculate_in_same_operation(self) -> None:
        snapshot = {
            "issuer_id": 117,
            "reported_runway_months": 2.0,
            "adjusted_runway_months": 2.0,
            "events": [],
        }
        with self.db.connect() as connection:
            proposal = create_transaction_correction(
                connection, self.payload(), actor="review-agent"
            )
            approve_transaction_correction(
                connection, proposal["id"], actor="autonomous-agent"
            )
            with (
                patch("capital_radar.radar_analysis.current_analysis", return_value=[snapshot]),
                patch("capital_radar.radar_analysis.recalculate_adjusted_runway") as recalculate,
            ):
                apply_transaction_correction(
                    connection, proposal["id"], actor="autonomous-agent"
                )
                self.assertEqual(recalculate.call_count, 1)
                self.assertEqual(
                    recalculate.call_args.kwargs["idempotency_key"],
                    f"transaction-grouping:{proposal['id']}",
                )
                self.assertEqual(
                    recalculate.call_args.kwargs["before_snapshot"], snapshot
                )
                revert_transaction_correction(
                    connection, proposal["id"], actor="autonomous-agent"
                )
                self.assertEqual(recalculate.call_count, 2)
                self.assertEqual(
                    recalculate.call_args.kwargs["idempotency_key"],
                    f"transaction-grouping-revert:{proposal['id']}",
                )

    def test_grouping_apply_rolls_back_when_runway_recalculation_fails(self) -> None:
        snapshot = {
            "issuer_id": 117,
            "reported_runway_months": 2.0,
            "adjusted_runway_months": 2.0,
            "events": [],
        }
        with self.db.connect() as connection:
            proposal = create_transaction_correction(
                connection, self.payload(), actor="review-agent"
            )
            approve_transaction_correction(
                connection, proposal["id"], actor="autonomous-agent"
            )
            with (
                patch("capital_radar.radar_analysis.current_analysis", return_value=[snapshot]),
                patch(
                    "capital_radar.radar_analysis.recalculate_adjusted_runway",
                    side_effect=ValueError("reported runway changed"),
                ),
                self.assertRaisesRegex(ValueError, "reported runway changed"),
            ):
                apply_transaction_correction(
                    connection, proposal["id"], actor="autonomous-agent"
                )
            status = connection.execute(
                "SELECT status FROM financing_transaction_corrections WHERE id=?",
                (proposal["id"],),
            ).fetchone()["status"]
            overrides = connection.execute(
                "SELECT COUNT(*) FROM financing_transaction_document_overrides WHERE correction_id=?",
                (proposal["id"],),
            ).fetchone()[0]
            self.assertEqual(status, "approved")
            self.assertEqual(overrides, 0)


if __name__ == "__main__":
    unittest.main()
