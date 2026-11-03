"""Disk space monitoring and automatic cleanup for Railway volume.

Monitors /app/data (or configured path) and triggers cleanup when usage
exceeds threshold. Pauses AI processing during cleanup.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .db import Database

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_MAX_GB = 4.5
DEFAULT_TARGET_GB = 3.0  # Clean down to this level
CHECK_INTERVAL_SECONDS = 300  # Check every 5 minutes


@dataclass
class DiskStatus:
    """Current disk usage status."""

    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_gb: float
    is_over_limit: bool
    cleanup_needed: bool


class DiskMonitor:
    """Monitors disk usage and triggers cleanup when needed."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        max_gb: float = DEFAULT_MAX_GB,
        target_gb: float = DEFAULT_TARGET_GB,
    ) -> None:
        self.settings = settings
        self.db = db
        self.max_bytes = int(max_gb * 1024**3)
        self.target_bytes = int(target_gb * 1024**3)
        self._lock = threading.Lock()
        self._cleanup_in_progress = False
        self._ai_paused = False
        self._last_check = 0.0

    def get_status(self) -> DiskStatus:
        """Get current disk usage status."""
        data_path = Path(self.settings.documents_path)
        if not data_path.exists():
            data_path.mkdir(parents=True, exist_ok=True)

        # Railway exposes the mounted volume path explicitly. Only there may
        # filesystem-wide usage be compared with the 5 GB volume quota. On a
        # developer machine that number describes the entire host drive and
        # previously caused unrelated disk usage to pause AI and purge data.
        stat = shutil.disk_usage(data_path)
        mount_value = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        on_managed_volume = False
        if mount_value:
            try:
                data_path.resolve().relative_to(Path(mount_value).resolve())
                on_managed_volume = True
            except ValueError:
                pass
        used_bytes = stat.used if on_managed_volume else self._managed_storage_bytes(data_path)
        used_gb = used_bytes / (1024**3)
        is_over = used_bytes > self.max_bytes
        cleanup_needed = is_over or (used_bytes > self.target_bytes and self._ai_paused)

        return DiskStatus(
            total_bytes=stat.total,
            used_bytes=used_bytes,
            free_bytes=stat.free,
            used_gb=round(used_gb, 2),
            is_over_limit=is_over,
            cleanup_needed=cleanup_needed,
        )

    def _managed_storage_bytes(self, documents_path: Path) -> int:
        """Measure only application-owned files outside a mounted Railway volume."""
        paths: set[Path] = set()
        if documents_path.exists():
            paths.update(path for path in documents_path.rglob("*") if path.is_file())
        database_path = Path(self.db.path)
        paths.update(Path(str(database_path) + suffix) for suffix in ("", "-wal", "-shm"))
        total = 0
        for path in paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def should_pause_ai(self) -> bool:
        """Check if AI processing should be paused due to disk space."""
        with self._lock:
            if self._cleanup_in_progress:
                return True
            status = self.get_status()
            if status.is_over_limit:
                self._ai_paused = True
                return True
            return self._ai_paused

    def resume_ai(self) -> None:
        """Resume AI processing after cleanup."""
        with self._lock:
            self._ai_paused = False

    def is_ai_paused(self) -> bool:
        """Check if AI is currently paused."""
        with self._lock:
            return self._ai_paused or self._cleanup_in_progress

    def cleanup_if_needed(self, force: bool = False) -> dict:
        """Run cleanup if disk usage exceeds threshold.

        Returns dict with cleanup results.
        """
        with self._lock:
            if self._cleanup_in_progress:
                return {"status": "cleanup_already_in_progress"}

            status = self.get_status()
            if not force and not status.cleanup_needed:
                return {
                    "status": "no_cleanup_needed",
                    "used_gb": status.used_gb,
                    "max_gb": self.max_bytes / (1024**3),
                }

            self._cleanup_in_progress = True
            self._ai_paused = True

        try:
            logger.warning(
                "disk_cleanup_started used_gb=%.2f max_gb=%.2f",
                status.used_gb,
                self.max_bytes / (1024**3),
            )

            results = {
                "status": "cleanup_completed",
                "before_gb": status.used_gb,
                "steps": [],
            }

            # Step 1: Delete old document_contents (extracted text)
            deleted_contents = self._purge_old_document_contents()
            results["steps"].append({"step": "document_contents", "deleted": deleted_contents})

            # Step 2: Delete old action_progress_events
            deleted_events = self._purge_old_progress_events()
            results["steps"].append({"step": "progress_events", "deleted": deleted_events})

            # Step 3: Delete old manual_action_runs
            deleted_runs = self._purge_old_manual_runs()
            results["steps"].append({"step": "manual_runs", "deleted": deleted_runs})

            # Step 4: Vacuum database to reclaim space
            self._vacuum_database()
            results["steps"].append({"step": "vacuum", "deleted": 0})

            # Check final status
            final_status = self.get_status()
            results["after_gb"] = final_status.used_gb
            results["freed_gb"] = round(status.used_gb - final_status.used_gb, 2)

            # Resume AI if we're under target
            if final_status.used_bytes < self.target_bytes:
                self._ai_paused = False
                results["ai_resumed"] = True
            else:
                results["ai_resumed"] = False
                logger.warning(
                    "disk_cleanup_incomplete still_over_target used_gb=%.2f target_gb=%.2f",
                    final_status.used_gb,
                    self.target_bytes / (1024**3),
                )

            logger.info(
                "disk_cleanup_completed before=%.2f after=%.2f freed=%.2f",
                results["before_gb"],
                results["after_gb"],
                results["freed_gb"],
            )

            return results

        finally:
            with self._lock:
                self._cleanup_in_progress = False

    def _purge_old_document_contents(self, keep_days: int = 7) -> int:
        """Delete extracted text older than keep_days."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - keep_days * 86400)
        )
        with self.db.connect() as c:
            cursor = c.execute(
                "DELETE FROM document_contents WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def _purge_old_progress_events(self, keep_days: int = 3) -> int:
        """Delete old progress events."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - keep_days * 86400)
        )
        with self.db.connect() as c:
            cursor = c.execute(
                "DELETE FROM action_progress_events WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def _purge_old_manual_runs(self, keep_days: int = 7) -> int:
        """Delete old manual action runs."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - keep_days * 86400)
        )
        with self.db.connect() as c:
            cursor = c.execute(
                "DELETE FROM manual_action_runs WHERE started_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def _vacuum_database(self) -> None:
        """Vacuum SQLite database to reclaim space."""
        with self.db.connect() as c:
            c.execute("VACUUM")


# Global instance
_disk_monitor: DiskMonitor | None = None


def get_disk_monitor(settings: Settings, db: Database) -> DiskMonitor:
    """Get or create global disk monitor instance."""
    global _disk_monitor
    if _disk_monitor is None:
        _disk_monitor = DiskMonitor(settings, db)
    return _disk_monitor


def check_disk_and_cleanup(settings: Settings, db: Database) -> dict:
    """Convenience function to check disk and cleanup if needed."""
    monitor = get_disk_monitor(settings, db)
    return monitor.cleanup_if_needed()
