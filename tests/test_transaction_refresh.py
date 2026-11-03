from __future__ import annotations

import unittest
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from capital_radar.transaction_refresh import TransactionEditorialWorker


def item(identifier="1", source=1, editorial_source="source"):
    return {"id": identifier, "published_at": "2026-09-06T10:00:00Z",
            "source_document_ids": [source], "editorial_source": editorial_source}


class TransactionRefreshTests(unittest.TestCase):
    def worker(self, pages, *, enabled=True):
        self.service = SimpleNamespace(enabled=enabled, process_item=Mock(return_value=True))
        self.clock = Mock(return_value=0)
        self.reader = Mock(side_effect=lambda page: pages[page])
        return TransactionEditorialWorker(
            None, SimpleNamespace(transaction_editorial_enabled=True),
            service=self.service, read_page=self.reader, clock=self.clock,
        )

    def test_disabled_worker_never_reads_or_generates(self):
        worker = self.worker({}, enabled=False)
        self.assertFalse(worker.tick())
        self.reader.assert_not_called()
        self.service.process_item.assert_not_called()

    def test_background_sweep_is_disabled_but_explicit_request_runs(self):
        first = item("requested")
        settings = SimpleNamespace(
            transaction_editorial_enabled=True,
            transaction_editorial_background_enabled=False,
        )
        done = threading.Event()
        service = SimpleNamespace(enabled=True, process_item=Mock(side_effect=lambda value: (done.set() or True)))
        reader = Mock()
        clock = Mock(return_value=0)
        worker = TransactionEditorialWorker(
            None, settings, service=service, read_page=reader,
            clock=clock,
        )
        self.assertFalse(worker.tick())
        reader.assert_not_called()
        self.assertTrue(worker.request(first))
        self.assertTrue(done.wait(2))
        service.process_item.assert_called_once_with(first)
        worker.stop()

    def test_one_item_per_tick_and_failed_item_has_cooldown(self):
        first, second = item("1"), item("2", source=2)
        worker = self.worker({1: {"items": [first, second], "total_pages": 1}})
        self.service.process_item.return_value = False
        worker.tick()
        self.service.process_item.assert_called_once_with(first)
        worker.tick()
        self.assertEqual(self.service.process_item.call_count, 2)
        self.service.process_item.assert_called_with(second)
        worker.tick()
        self.assertEqual(self.service.process_item.call_count, 2)
        self.clock.return_value = 301
        worker.tick()
        self.assertEqual(self.service.process_item.call_count, 3)

    def test_outcome_source_is_eligible_without_waiting_for_old_cooldown(self):
        pages = {1: {"items": [item("1")], "total_pages": 1}}
        worker = self.worker(pages)
        worker.tick()
        pages[1]["items"] = [item("1", source=3)]
        worker.tick()
        self.assertEqual(self.service.process_item.call_count, 2)
        self.service.process_item.assert_called_with(pages[1]["items"][0])

    def test_explicit_request_runs_before_paginated_history(self):
        requested = item("requested", source=99)
        worker = self.worker({1: {"items": [item("page")], "total_pages": 1}})
        self.assertTrue(worker.request(requested))
        worker.tick()
        self.service.process_item.assert_called_once_with(requested)

    def test_cached_front_page_does_not_starve_history(self):
        old = item("old", source=2)
        pages = {1: {"items": [item(editorial_source="ai")], "total_pages": 3},
                 2: {"items": [], "total_pages": 3},
                 3: {"items": [old], "total_pages": 3}}
        worker = self.worker(pages)
        worker.tick()
        worker.tick()
        self.service.process_item.assert_called_once_with(old)

    def test_request_tracks_both_passes_and_deduplicates_active_clicks(self):
        requested = item("requested")
        worker = self.worker({})
        states = []
        self.service.process_item.side_effect = lambda _: (states.append(worker.request_status("requested")["status"]) or True)
        self.service.enrich_missing_fields = Mock(side_effect=lambda _: states.append(worker.request_status("requested")["status"]))
        worker.request(requested)
        initial = worker.request_status("requested")
        self.assertEqual(initial["status"], "queued")
        worker.request(requested)
        self.assertEqual(worker.request_status("requested"), initial)
        worker.tick()
        self.assertEqual(states, ["running", "enriching"])
        self.assertEqual(worker.request_status("requested")["status"], "completed")
        self.reader.assert_not_called()

    def test_enrichment_failure_preserves_commentary_and_new_request_has_new_id(self):
        requested = item("requested")
        worker = self.worker({})
        self.service.enrich_missing_fields = Mock(side_effect=RuntimeError("failed"))
        worker.request(requested)
        old_id = worker.request_status("requested")["request_id"]
        self.assertTrue(worker.tick())
        self.assertEqual(worker.request_status("requested")["status"], "failed")
        worker.request(requested)
        self.assertNotEqual(worker.request_status("requested")["request_id"], old_id)
        self.assertEqual(worker.request_status("requested")["status"], "queued")

    def test_incomplete_market_window_is_eligible_for_refresh(self):
        service = SimpleNamespace(enabled=True, process_item=Mock(return_value=True))
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        database = MagicMock()
        database.connect.return_value.__enter__.return_value = connection
        worker = TransactionEditorialWorker(
            database, SimpleNamespace(transaction_editorial_enabled=True),
            service=service, read_page=Mock(), clock=Mock(return_value=0),
        )

        worker._refresh_market_data({"issuer_id": 7, "source_document_ids": [42]})

        query = connection.execute.call_args.args[0]
        self.assertIn("metrics.complete=0", query)
        self.assertEqual(connection.execute.call_args.args[1], (7, 42))


if __name__ == "__main__":
    unittest.main()
