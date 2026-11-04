"""Periodisk MFN-bevakning av bevakade bolag.

Ersätter den gamla NewsSyncWorker/NewsRepository-kedjan (som också bar
nyhetskommentarer). Varje varv hämtar bolagets MFN-sida för alla aktiva
bevakade bolag och sparar nya dokument via `Database.upsert_document`.
Inga AI-anrop görs här; tolkningen sköts av AnalysisWorker.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import Settings
from .db import Database
from .http import HttpClient
from .manual_actions import ManualActionService
from .models import DocumentCandidate
from .normalization import utc_now
from .sources.mfn import MfnDiscoveryAdapter

logger = logging.getLogger(__name__)


class MfnMonitor:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        actions: ManualActionService,
        *,
        interval_seconds: float | None = None,
        per_issuer_timeout: float = 30.0,
    ) -> None:
        self.database = database
        self.settings = settings
        self.actions = actions
        self.interval_seconds = max(60.0, float(interval_seconds or settings.mfn_monitor_interval_seconds))
        self.per_issuer_timeout = per_issuer_timeout
        self.channels = max(1, min(4, settings.mfn_parallel_channels))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="capital-mfn-monitor", daemon=True)
        self.last_run: dict[str, Any] | None = None

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": "running" if self._thread.is_alive() else "stopped",
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
        }

    def watched_issuers(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.id, i.canonical_name FROM issuer_watchlist w
                JOIN issuers i ON i.id=w.issuer_id
                WHERE w.active=1 AND EXISTS (
                    SELECT 1 FROM securities s WHERE s.issuer_id=i.id AND s.active=1)
                ORDER BY i.canonical_name COLLATE NOCASE
                """
            ).fetchall()
        return [{"id": int(row[0]), "canonical_name": str(row[1])} for row in rows]

    def run_once(self) -> dict[str, Any]:
        started = time.monotonic()
        summary: dict[str, Any] = {"started_at": utc_now(), "issuers": 0, "seen": 0, "created": 0, "failed": 0}
        issuers = self.watched_issuers()
        summary["issuers"] = len(issuers)
        with ThreadPoolExecutor(max_workers=self.channels, thread_name_prefix="capital-mfn") as executor:
            futures = {executor.submit(self._sync_issuer, issuer): issuer for issuer in issuers}
            for future in as_completed(futures):
                try:
                    seen, created = future.result()
                    summary["seen"] += seen
                    summary["created"] += created
                except Exception as error:
                    summary["failed"] += 1
                    issuer = futures[future]
                    logger.warning("mfn_issuer_failed issuer=%s error=%s", issuer["id"], type(error).__name__)
                if self._stop.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
        summary["finished_at"] = utc_now()
        summary["duration_seconds"] = round(time.monotonic() - started, 1)
        self.last_run = summary
        logger.info("mfn_monitor_run %s", summary)
        return summary

    def _sync_issuer(self, issuer: dict[str, Any]) -> tuple[int, int]:
        if self._stop.is_set():
            return 0, 0
        slug, url = self.actions._resolve_mfn_identity(issuer["id"], issuer["canonical_name"])
        http = HttpClient(
            user_agent=self.settings.user_agent,
            timeout=max(3.0, min(self.settings.http_timeout, self.per_issuer_timeout)),
            min_interval=self.settings.min_request_interval,
            retries=1,
        )
        result = MfnDiscoveryAdapter(http).fetch_issuer(
            url, issuer_name=issuer["canonical_name"], issuer_slug=slug, limit=10_000,
        )
        created = 0
        for source in result.items:
            candidate = DocumentCandidate(
                source_id=source.source_id, external_id=source.external_id,
                issuer_name=issuer["canonical_name"], title=source.title,
                canonical_url=source.canonical_url, published_at=source.published_at,
                language=source.language, category=source.category,
                attachments=source.attachments,
                metadata={**source.metadata, "source_issuer_name": source.issuer_name,
                          "collection_policy": "news_watchlist_only"},
            )
            outcome, _ = self.database.upsert_document(candidate, universe_only=True)
            created += int(outcome == "created")
        return len(result.items), created

    def _loop(self) -> None:
        # Kort fördröjning så att appen hinner starta innan första varvet.
        if self._stop.wait(30):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("mfn_monitor_run_failed")
            self._stop.wait(self.interval_seconds)
