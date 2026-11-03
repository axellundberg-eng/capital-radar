"""Bounded editorial refresh over the existing, locally stored MFN sources.

This worker never discovers news or updates financial analysis. The regular MFN
workers own discovery; missing source text uses the existing safe news extractor.
AgentRunner owns provider execution, audit and caching.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from typing import Any, Callable

from .transaction_editorial import TransactionEditorialService, apply_cached_editorial
from .transaction_feed import get_transaction_feed


LOGGER = logging.getLogger(__name__)


class TransactionEditorialWorker:
    """Prefer fresh publications, then progressively enrich historical pages."""

    def __init__(
        self, database: Any, settings: Any, *, service: Any = None,
        interval_seconds: float = 30.0, retry_seconds: float = 300.0,
        read_page: Callable[[int], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.settings = settings
        self.service = service or TransactionEditorialService(database, settings)
        self.interval_seconds = max(1.0, min(interval_seconds, 60.0))
        self.retry_seconds = max(30.0, retry_seconds)
        self.read_page = read_page or self._read_page
        self.clock = clock
        self._attempts: dict[tuple, float] = {}
        self._requested: dict[tuple, dict[str, Any]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._request_lock = threading.Lock()
        self._history_page = 2
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="capital-transaction-editorial", daemon=True,
        )

    @property
    def enabled(self) -> bool:
        return bool(
            getattr(self.settings, "transaction_editorial_enabled", True)
            and self.service.enabled
        )

    def start(self) -> None:
        if (self.enabled
                and getattr(self.settings, "transaction_editorial_background_enabled", False)
                and not self._thread.is_alive()):
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def request(self, item: dict[str, Any]) -> bool:
        """Prioritise a user-opened transaction; HTTP never calls the provider."""
        if not self.enabled or not isinstance(item, dict):
            return False
        with self._request_lock:
            existing = self._requests.get(str(item["id"]), {})
            if existing.get("status") in {"queued", "running", "enriching"}:
                return True
            self._requests[str(item["id"])] = {
                "request_id": uuid.uuid4().hex, "status": "queued",
            }
            self._requested[self._key(item)] = dict(item)
        # Explicitly opened deals must still be processed when the costly
        # historical/background sweep is disabled.
        if (hasattr(self.settings, "transaction_editorial_background_enabled")
                and not self._thread.is_alive() and not self._stop.is_set()):
            self._thread.start()
        return True

    def request_status(self, item_id: str) -> dict[str, Any] | None:
        with self._request_lock:
            value = self._requests.get(str(item_id))
            return dict(value) if value else None

    def _set_status(self, item: dict[str, Any], status: str) -> None:
        with self._request_lock:
            value = self._requests.get(str(item["id"]))
            if value is not None:
                value["status"] = status

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # Never log source text, provider responses or credentials.
                LOGGER.warning("transaction_editorial_tick_failed type=%s", type(exc).__name__)
            if self._stop.wait(self.interval_seconds):
                return

    def _read_page(self, page: int) -> dict[str, Any]:
        path = self.database.path.resolve().as_posix()
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            payload = get_transaction_feed(connection, page=page, page_size=10)
            payload["items"] = [
                apply_cached_editorial(connection, item) for item in payload["items"]
            ]
            return payload

    def tick(self) -> bool:
        """Attempt at most one short AI rewrite, without holding a DB connection."""
        if not self.enabled or self._stop.is_set():
            return False
        now = self.clock()
        self._attempts = {key: at for key, at in self._attempts.items() if now - at < self.retry_seconds}
        with self._request_lock:
            candidate = next(iter(self._requested.values()), None)
            if candidate is not None:
                self._requested.pop(self._key(candidate), None)
        background_enabled = getattr(
            self.settings, "transaction_editorial_background_enabled", True
        )
        if candidate is None and not background_enabled:
            return False
        latest = self.read_page(1) if candidate is None else {}
        candidate = candidate or self._candidate(latest)
        if candidate is None and latest.get("total_pages", 0) > 1:
            self._history_page = min(self._history_page, latest["total_pages"])
            history = self.read_page(self._history_page)
            candidate = self._candidate(history)
            if candidate is None:
                self._history_page = 2 if self._history_page >= latest["total_pages"] else self._history_page + 1
        if candidate is None:
            return False
        self._attempts[self._key(candidate)] = now
        self._set_status(candidate, "running")
        try:
            self._refresh_market_data(candidate)
        except Exception as exc:  # market data must never block commentary
            LOGGER.info("transaction_market_data_refresh_failed type=%s", type(exc).__name__)
        # An unsuccessful attempt receives a cooldown; new source versions get
        # their own key and can be processed immediately on the next tick.
        try:
            published = bool(self.service.process_item(candidate))
        except Exception:
            self._set_status(candidate, "failed")
            raise
        if published:
            self._set_status(candidate, "enriching")
            try:
                # A second, Flash-priced pass may fill only empty, cited deal
                # fields after the source-bound editorial has been published.
                enrich = getattr(self.service, "enrich_missing_fields", None)
                if callable(enrich):
                    enrich(candidate)
            except Exception as exc:
                LOGGER.info("transaction_fact_enrichment_failed type=%s", type(exc).__name__)
                self._set_status(candidate, "failed")
                return published
        self._set_status(candidate, "completed" if published else "failed")
        return published

    def _refresh_market_data(self, item: dict[str, Any]) -> None:
        """Fetch Yahoo daily bars for an evidenced completed outcome when absent."""
        if self.database is None:
            return
        source_ids = [int(value) for value in item.get("source_document_ids", []) if str(value).isdigit()]
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT event.id,
                           COALESCE(event.effective_date, event.announcement_date) AS outcome_date
                    FROM financing_events event
                    JOIN financing_event_evidence evidence
                      ON evidence.financing_event_id=event.id
                    LEFT JOIN financing_outcome_market_metrics metrics
                      ON metrics.financing_event_id=event.id
                    WHERE event.issuer_id=? AND evidence.document_id IN ({placeholders})
                      AND event.status='completed'
                      AND COALESCE(event.effective_date, event.announcement_date) IS NOT NULL
                       AND (metrics.financing_event_id IS NULL OR metrics.complete=0)""",
                (item.get("issuer_id"), *source_ids),
            ).fetchall()
        if not rows:
            return
        from .yahoo_market_data import YahooFinanceMarketDataProvider, persist_post_outcome_metrics

        provider = YahooFinanceMarketDataProvider(self.database)
        for row in rows[:1]:
            metrics = provider.post_outcome_metrics(int(item["issuer_id"]), str(row["outcome_date"]))
            if metrics is not None:
                persist_post_outcome_metrics(self.database, int(row["id"]), metrics)

    @staticmethod
    def _key(item: dict[str, Any]) -> tuple:
        return (str(item["id"]), str(item.get("published_at")),
                tuple(str(value) for value in item.get("source_document_ids", [])))

    def _candidate(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return next((item for item in payload.get("items", [])
                     if item.get("editorial_source") != "ai"
                     and self._key(item) not in self._attempts), None)
