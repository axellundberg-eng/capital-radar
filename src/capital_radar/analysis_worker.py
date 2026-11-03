from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from typing import Any

from .db import is_sqlite_busy_error
from .disk_monitor import get_disk_monitor
from .manual_actions import ManualActionService


LOGGER = logging.getLogger("bergs.ai")


class AnalysisWorker:
    def __init__(
        self,
        actions: ManualActionService,
        *,
        poll_interval: float = 0.5,
        worker_threads: int = 1,
    ) -> None:
        self.actions = actions
        self.poll_interval = max(0.1, poll_interval)
        # Flera worker-trådar kör claimed actions parallellt (bounded av
        # worker_threads). queue.Queue är trådsäker; claim_action är en
        # atomisk lease så samma action kan aldrig köras av två trådar.
        self.worker_threads = max(1, worker_threads)
        self._queue: queue.PriorityQueue[tuple[int, int | None]] = queue.PriorityQueue()
        self._scheduled: set[int] = set()
        self._scheduled_lock = threading.Lock()
        self._stop = threading.Event()
        self._last_auto_analysis_at: float = 0.0
        self._workers = [
            threading.Thread(
                target=self._run,
                name=f"bergs-ai-worker-{index}",
                daemon=True,
            )
            for index in range(self.worker_threads)
        ]
        self._watchdog = threading.Thread(
            target=self._watch,
            name="bergs-ai-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        recovered = self.actions.recover_expired_actions()
        if recovered["actions"] or recovered["jobs"]:
            LOGGER.warning("ai_recovered %s", recovered)
        resumable = self.actions.resume_screening_batches()
        pending = self.actions.pending_action_ids()
        for action_id in dict.fromkeys((*resumable, *pending)):
            self._schedule(action_id)
        for worker in self._workers:
            worker.start()
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            self._queue.put((99, None))
        for worker in self._workers:
            worker.join(timeout=2)
        self._watchdog.join(timeout=2)

    def resume_force_actions(self, action_ids: list[int] | None = None) -> dict[str, Any]:
        """Re-schedule queued force actions without creating new work."""
        candidates = action_ids if action_ids is not None else self.actions.pending_action_ids()
        resumed: list[int] = []
        skipped: list[dict[str, Any]] = []
        for action_id in candidates:
            try:
                status = self.actions.action_status(int(action_id))
            except KeyError:
                skipped.append({"id": int(action_id), "reason": "not_found"})
                continue
            if not str(status.get("action_type", "")).startswith("force_reinterpret"):
                skipped.append({"id": int(action_id), "reason": "not_force_action"})
                continue
            if status.get("status") != "queued":
                skipped.append({"id": int(action_id), "reason": f"status:{status.get('status')}"})
                continue
            self._schedule(int(action_id))
            resumed.append(int(action_id))
        return {"resumed": resumed, "skipped": skipped}

    def enqueue(
        self, issuer_id: int, *, trigger_kind: str = "manual"
    ) -> dict[str, Any]:
        if trigger_kind != "manual":
            raise ValueError(
                "Nya Kimi-anrop får endast startas från ett manuellt val."
            )
        action = self.actions.enqueue_interpret(issuer_id)
        if action["status"] == "queued":
            self._schedule(int(action["id"]))
        LOGGER.info(
            "ai_enqueued action=%s issuer=%s created=%s",
            action["id"],
            issuer_id,
            action.get("created"),
        )
        return action

    def status(self, action_id: int) -> dict[str, Any]:
        return self.actions.action_status(action_id)

    def start_screening_batch(
        self,
        *,
        refresh_mfn: bool = True,
        max_api_calls: int = 10,
        trigger_kind: str = "manual",
    ) -> dict[str, Any]:
        if trigger_kind != "manual":
            raise ValueError(
                "Kimi-batcher får endast startas manuellt."
            )
        batch = self.actions.start_screening_batch(
            refresh_mfn=refresh_mfn,
            max_api_calls=max_api_calls,
        )
        next_action_id = batch.pop("_next_action_id", None)
        if next_action_id is not None:
            self._schedule(int(next_action_id))
        return batch

    def screening_filter_preview(self, stage: str) -> dict[str, Any]:
        return self.actions.screening_filter_preview(stage)

    def screening_batch_status(
        self, batch_id: int | None = None
    ) -> dict[str, Any] | None:
        if batch_id is None:
            return self.actions.latest_screening_batch()
        return self.actions.screening_batch_status(batch_id)

    def stop_screening_batch(self, batch_id: int) -> dict[str, Any]:
        return self.actions.stop_screening_batch(batch_id)

    def set_screening_override(
        self, issuer_id: int, *, eligible: bool
    ) -> dict[str, Any]:
        result = self.actions.set_screening_override(
            issuer_id, eligible=eligible
        )
        next_action_id = result.pop("_next_action_id", None)
        if next_action_id is not None:
            self._schedule(int(next_action_id))
        return result

    def _schedule(self, action_id: int) -> None:
        with self._scheduled_lock:
            if action_id in self._scheduled:
                return
            self._scheduled.add(action_id)
        try:
            action_type = str(self.actions.action_status(action_id).get("action_type", ""))
        except KeyError:
            return
        priority = 0 if action_type.startswith("force_reinterpret") else 1
        self._queue.put((priority, action_id))

    def _run(self) -> None:
        # Disk monitor hämtas lazily för att undvika cirkulära importer
        disk_monitor = None
        if hasattr(self.actions, 'db') and hasattr(self.actions, 'settings'):
            try:
                disk_monitor = get_disk_monitor(self.actions.settings, self.actions.db)
            except Exception:
                pass  # Fortsätt utan disk-övervakning om det misslyckas

        while not self._stop.is_set():
            # Kolla disk space innan vi plockar nytt jobb
            if disk_monitor and disk_monitor.should_pause_ai():
                LOGGER.warning("ai_paused_disk_full waiting_for_cleanup")
                if self._stop.wait(timeout=60):  # Vänta 60s eller tills stop
                    return
                # Försök städa om vi fortfarande är över gränsen
                if disk_monitor.should_pause_ai():
                    disk_monitor.cleanup_if_needed()
                continue

            try:
                _, action_id = self._queue.get(timeout=self.poll_interval)
            except queue.Empty:
                continue
            if action_id is None:
                return
            with self._scheduled_lock:
                self._scheduled.discard(action_id)
            claimed = self.actions.claim_action(action_id)
            if not claimed:
                try:
                    status = self.actions.action_status(action_id)
                except KeyError:
                    continue
                if status["status"] == "queued":
                    if self._stop.wait(self.poll_interval):
                        return
                    self._schedule(action_id)
                else:
                    next_action_id = (
                        self.actions.record_screening_action(action_id)
                    )
                    if next_action_id is not None:
                        self._schedule(next_action_id)
                continue
            lease_token, deadline_at = claimed
            started_at = time.monotonic()
            LOGGER.info(
                "ai_started action=%s deadline=%s",
                action_id,
                deadline_at,
            )
            self.actions.run_claimed_action(
                action_id,
                lease_token,
                deadline_at,
            )
            status = self.actions.action_status(action_id)
            result = status.get("result") or {}
            analysis = result.get("analysis") or {}
            documents = analysis.get("documents") or []
            first_document = documents[0] if documents else {}
            prepared = result.get("prepared")
            prepared_documents = (
                prepared.get("documents") or [] if isinstance(prepared, dict) else []
            )
            first_prepared = (
                prepared_documents[0] if prepared_documents else {}
            )
            usage = first_document.get("usage") or {}
            LOGGER.info(
                "ai_finished action=%s issuer=%s status=%s phase=%s "
                "job=%s document=%s attachment=%s duration_seconds=%.3f "
                "deadline=%s completed=%s prompt_tokens=%s "
                "completion_tokens=%s cached_tokens=%s total_tokens=%s error=%s",
                action_id,
                status["issuer_id"],
                status["status"],
                status["phase"],
                first_document.get("job_id"),
                first_document.get("document_id")
                or first_prepared.get("document_id"),
                first_prepared.get("selected_source_url"),
                time.monotonic() - started_at,
                deadline_at,
                analysis.get("completed"),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("cached_tokens"),
                usage.get("total_tokens"),
                status.get("error"),
            )
            next_action_id = self.actions.record_screening_action(action_id)
            if next_action_id is not None:
                self._schedule(next_action_id)

    def _watch(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                recovered = self.actions.recover_expired_actions()
            except sqlite3.OperationalError as exc:
                if not is_sqlite_busy_error(exc):
                    # Schema-/programmeringsfel får inte döljas.
                    raise
                # Ett tillfälligt SQLite-lås får inte döda watchdog-tråden.
                # Nästa tick försöker igen.
                LOGGER.warning("ai_watchdog_locked %s", exc)
                continue
            if recovered["actions"] or recovered["jobs"]:
                LOGGER.warning("ai_watchdog_recovered %s", recovered)
            for action_id in self.actions.resume_screening_batches():
                self._schedule(action_id)
            self._auto_analysis_tick()

    def _auto_analysis_tick(self) -> None:
        """Toppa upp analyskön mot målet (nya rapporter först).

        Körs från watchdog-loopen men throttlas till
        auto_analysis_interval_seconds. Håller kön konstant fylld mot
        auto_analysis_queue_target: väljer bara så många nya kandidater
        som behövs för att nå målet. Blockerar aldrig — köar bara.
        """
        settings = getattr(self.actions, "settings", None)
        if settings is None or not getattr(
            settings, "auto_analysis_enabled", False
        ):
            return
        now = time.monotonic()
        if now - self._last_auto_analysis_at < settings.auto_analysis_interval_seconds:
            return
        self._last_auto_analysis_at = now
        queue_target = max(
            1, int(getattr(settings, "auto_analysis_queue_target", 20))
        )
        try:
            active_count = self.actions.count_active_interpret_actions()
        except Exception:
            LOGGER.exception("auto_analysis_count_active_failed")
            return
        needed = max(0, queue_target - active_count)
        if needed <= 0:
            return
        try:
            candidates = self.actions.select_analysis_candidates(
                needed,
                max_attempts=settings.auto_analysis_max_attempts,
            )
        except Exception:
            LOGGER.exception("auto_analysis_select_failed")
            return
        if not candidates:
            return
        enqueued = 0
        for issuer_id in candidates:
            try:
                action = self.actions.enqueue_interpret(
                    issuer_id, trigger_kind="automation"
                )
                if action["status"] == "queued":
                    self._schedule(int(action["id"]))
                    enqueued += 1
            except Exception:
                LOGGER.exception(
                    "auto_analysis_enqueue_failed issuer=%s", issuer_id
                )
        if enqueued:
            LOGGER.info(
                "auto_analysis_topped_up enqueued=%s active=%s target=%s issuers=%s",
                enqueued,
                active_count,
                queue_target,
                candidates,
            )
