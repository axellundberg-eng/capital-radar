from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import test_transaction_value_corrections as value_fixtures
from capital_radar.transaction_case import get_transaction_case
from capital_radar.radar_analysis import current_analysis
from capital_radar.transaction_feed import get_transaction_feed
from capital_radar.transaction_value_corrections import (
    create_transaction_value_correction, approve_transaction_value_correction,
    apply_transaction_value_correction,
)


class TransactionCaseTests(unittest.TestCase):
    setUp = value_fixtures.TransactionValueCorrectionTests.setUp

    def test_case_reads_stored_text_and_complete_audit_without_writes(self):
        with self.db.connect() as connection:
            transaction_id = current_analysis(connection, [self.issuer_id])[0]["events"][0]["transaction_id"]
            before = list(connection.iterdump())
            case = get_transaction_case(connection, transaction_id)
            self.assertEqual(list(connection.iterdump()), before)
            self.assertEqual(case["documents"][0]["contents"][0]["extracted_text"], self.source)
            self.assertTrue(case["documents"][0]["in_selected_transaction"])
            self.assertTrue(case["source_policy"]["reported_runway_immutable"])
            self.assertEqual(case["issuer_transactions"][0]["transaction_id"], transaction_id)
            original_reported = case["analysis"]["reported_runway_months"]
            proposal = create_transaction_value_correction(connection, {
                "transaction_id": transaction_id, "issuer_id": self.issuer_id,
                "field_name": "amount", "expected_previous_value": case["transaction"]["amount"],
                "proposed_value": 36_000_000, "source_document_id": self.document_id,
                "source_excerpt": self.source, "confidence": 1, "rationale": "Verifierat belopp i källa",
            }, actor="test-agent")
            approve_transaction_value_correction(connection, proposal["id"], actor="test-agent")
            apply_transaction_value_correction(connection, proposal["id"], actor="test-agent")
            before = list(connection.iterdump())
            updated = get_transaction_case(connection, transaction_id)
            self.assertEqual(list(connection.iterdump()), before)
            self.assertEqual(updated["transaction"]["amount"], 36_000_000)
            self.assertEqual(updated["audit"]["value_corrections"][0]["status"], "applied")
            self.assertEqual(updated["analysis"]["reported_runway_months"], original_reported)
            self.assertEqual(updated["documents"], case["documents"])

    def test_missing_transaction_does_not_write(self):
        with self.db.connect() as connection:
            before = list(connection.iterdump())
            with self.assertRaises(KeyError):
                get_transaction_case(connection, "transaction:999:rights_issue:999")
            self.assertEqual(list(connection.iterdump()), before)

    def test_source_text_cap_is_explicit_and_does_not_modify_document(self):
        from capital_radar.transaction_case import MAX_SOURCE_CHARS
        with self.db.connect() as connection:
            long_text = self.source + " " * MAX_SOURCE_CHARS
            connection.execute("UPDATE document_contents SET extracted_text=? WHERE document_id=?", (long_text, self.document_id))
            transaction_id = current_analysis(connection, [self.issuer_id])[0]["events"][0]["transaction_id"]
            case = get_transaction_case(connection, transaction_id)
            content = case["documents"][0]["contents"][0]
            self.assertEqual(len(content["extracted_text"]), MAX_SOURCE_CHARS)
            self.assertEqual(content["omitted_char_count"], len(self.source))
            self.assertEqual(case["coverage"]["omitted_source_chars"], len(self.source))
            self.assertEqual(connection.execute("SELECT extracted_text FROM document_contents WHERE document_id=?", (self.document_id,)).fetchone()[0], long_text)

    def test_case_keeps_source_reads_scoped_to_its_issuer(self):
        import capital_radar.transaction_feed as feed_module

        with self.db.connect() as connection:
            feed_module._TRANSACTION_CACHE.clear()
            original = feed_module._build_transaction_items
            with patch.object(feed_module, "_build_transaction_items", wraps=original) as build:
                transaction_id = get_transaction_feed(connection)["items"][0]["id"]
                case = get_transaction_case(connection, transaction_id)
            self.assertEqual(case["transaction"]["id"], transaction_id)
            # Detail and analysis deliberately read the selected issuer from
            # source. They must not rebuild the entire market graph merely
            # because the global feed cache is populated.
            global_reads = [call for call in build.call_args_list
                            if call.kwargs.get("issuer_ids") is None]
            scoped_reads = [call for call in build.call_args_list
                            if call.kwargs.get("issuer_ids") is not None]
            self.assertEqual(len(global_reads), 1)
            self.assertTrue(scoped_reads)
            self.assertLessEqual(build.call_count, 3)
            self.assertTrue(all(call.kwargs["issuer_ids"] == [self.issuer_id]
                                for call in scoped_reads))
