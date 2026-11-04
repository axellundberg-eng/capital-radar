from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .config import Settings
from .db import Database
from .http import HttpClient
from .services.universe import sync_universe
from .sources.nasdaq import NasdaqUniverseAdapter
from .sources.ngm import NgmUniverseAdapter


LOGGER = logging.getLogger(__name__)


class UniverseBootstrapWorker:
    """Populate a new deployment without delaying the HTTP server startup."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        sync: Callable[[], Any] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self._sync = sync or self._sync_live
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {"status": "idle", "error": None}

    def start_if_empty(self) -> bool:
        if self.database.status().get("latest_snapshot") is not None:
            with self._lock:
                self._status = {"status": "skipped_existing", "error": None}
            LOGGER.info("Universe bootstrap skipped: snapshot already exists")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._status = {"status": "running", "error": None}
            self._thread = threading.Thread(
                target=self._run,
                name="universe-bootstrap",
                daemon=True,
            )
            self._thread.start()
        LOGGER.info("Universe bootstrap started in background")
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            summary = self._sync()
        except Exception as exc:
            LOGGER.exception("Universe bootstrap failed")
            with self._lock:
                self._status = {"status": "failed", "error": str(exc)}
            return
        with self._lock:
            self._status = {
                "status": "completed",
                "error": None,
                "result": (
                    summary.to_dict()
                    if hasattr(summary, "to_dict")
                    else summary
                ),
            }
        LOGGER.info("Universe bootstrap completed")

    def _sync_live(self) -> Any:
        http = HttpClient(
            user_agent=self.settings.user_agent,
            timeout=self.settings.http_timeout,
            min_interval=self.settings.min_request_interval,
        )
        return sync_universe(
            self.database,
            nasdaq=NasdaqUniverseAdapter(http),
            ngm=NgmUniverseAdapter(http),
        )
