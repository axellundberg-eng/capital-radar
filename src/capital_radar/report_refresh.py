from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .analysis_store import AnalysisStore
from .config import Settings
from .db import Database
from .extraction import analyze_documents
from .http import HttpClient
from .manual_actions import ManualActionService
from .normalization import utc_now
from .services.documents import prepare_documents
from .sources.mfn import MfnDiscoveryAdapter, MfnIssuerPageEmptyError


LOGGER = logging.getLogger("bergs.report-refresh")
TERMINAL_ITEM_STATUSES = {
    "ready",
    "no_report",
    "skipped_recent",
    "failed",
}


class ReportRefreshService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        actions: ManualActionService | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.store = AnalysisStore(db)
        self.actions = actions or ManualActionService(db, settings)
        self.parallel_channels = max(
            1,
            min(8, int(getattr(settings, "mfn_parallel_channels", 4))),
        )

    def start_or_resume(
        self,
        *,
        trigger_kind: str,
        force_backfill: bool = False,
    ) -> dict[str, Any]:
        if trigger_kind not in {
            "manual",
            "manual_cli",
            "scheduled",
            "startup",
        }:
            raise ValueError(f"Ogiltig rapporttrigger: {trigger_kind}")
        now = utc_now()
        issuer_ids = self._active_issuer_ids()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT id, status, collection_scope
                FROM report_refresh_runs
                WHERE status IN ('queued', 'running', 'paused')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if (
                active
                and str(active["collection_scope"]) != "all_active_issuers"
                and str(active["status"]) != "running"
            ):
                connection.execute(
                    """
                    UPDATE report_refresh_runs
                    SET status='superseded', completed_at=?, updated_at=?,
                        error=?
                    WHERE id=?
                    """,
                    (
                        now,
                        now,
                        (
                            "Ersatt av komplett MFN-insamling för samtliga "
                            "aktiva bolag"
                        ),
                        int(active["id"]),
                    ),
                )
                active = None
            if active:
                run_id = int(active["id"])
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO report_refresh_items (
                        run_id, issuer_id, status, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (
                        (run_id, issuer_id, now, now)
                        for issuer_id in issuer_ids
                    ),
                )
                if force_backfill:
                    connection.execute(
                        """
                        UPDATE report_refresh_runs
                        SET force_backfill=1, updated_at=?
                        WHERE id=?
                        """,
                        (now, run_id),
                    )
                connection.execute(
                    """
                    UPDATE report_refresh_runs
                    SET total_count=(
                        SELECT COUNT(*) FROM report_refresh_items
                        WHERE run_id=?
                    ), updated_at=?
                    WHERE id=?
                    """,
                    (run_id, now, run_id),
                )
                created = False
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO report_refresh_runs (
                        trigger_kind, collection_scope, force_backfill,
                        status, total_count, created_at, updated_at
                    ) VALUES (?, 'all_active_issuers', ?, 'queued', ?, ?, ?)
                    """,
                    (
                        trigger_kind,
                        int(force_backfill),
                        len(issuer_ids),
                        now,
                        now,
                    ),
                )
                run_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO report_refresh_items (
                        run_id, issuer_id, status, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (
                        (run_id, issuer_id, now, now)
                        for issuer_id in issuer_ids
                    ),
                )
                created = True
        payload = self.status(run_id)
        payload["created"] = created
        return payload

    def latest(self) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM report_refresh_runs
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self.status(int(row["id"])) if row else None

    def status(self, run_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT * FROM report_refresh_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KeyError(run_id)
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(documents_seen), 0) AS news_seen,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)
                           AS pending,
                       SUM(CASE WHEN status='running' THEN 1 ELSE 0 END)
                           AS running,
                       SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END)
                           AS ready,
                       SUM(CASE WHEN status='no_report' THEN 1 ELSE 0 END)
                           AS no_report,
                       SUM(CASE WHEN status='skipped_recent' THEN 1 ELSE 0 END)
                           AS skipped_recent,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)
                           AS failed
                FROM report_refresh_items WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
            current_rows = connection.execute(
                """
                SELECT ri.issuer_id, i.canonical_name AS issuer,
                       ri.status, ri.attempt_count, ri.updated_at
                FROM report_refresh_items ri
                JOIN issuers i ON i.id=ri.issuer_id
                WHERE ri.run_id=? AND ri.status='running'
                ORDER BY ri.id LIMIT ?
                """,
                (run_id, self.parallel_channels),
            ).fetchall()
            recent = connection.execute(
                """
                SELECT ri.issuer_id, i.canonical_name AS issuer,
                       ri.status, ri.documents_seen, ri.documents_prepared,
                       ri.reason, ri.error, ri.updated_at
                FROM report_refresh_items ri
                JOIN issuers i ON i.id=ri.issuer_id
                WHERE ri.run_id=? AND ri.status NOT IN ('pending', 'running')
                ORDER BY ri.updated_at DESC, ri.id DESC
                LIMIT 20
                """,
                (run_id,),
            ).fetchall()
        payload = dict(run)
        progress = {key: int(value or 0) for key, value in dict(counts).items()}
        progress["finished"] = (
            progress["total"] - progress["pending"] - progress["running"]
        )
        payload["progress"] = progress
        current_items = [dict(row) for row in current_rows]
        payload["current"] = current_items[0] if current_items else None
        payload["current_items"] = current_items
        payload["parallel_channels"] = self.parallel_channels
        payload["recent_items"] = [dict(row) for row in recent]
        raw_global = payload.pop("global_discovery_json")
        payload["global_discovery"] = (
            json.loads(raw_global) if raw_global else None
        )
        return payload

    def run_slice(
        self,
        run_id: int,
        *,
        max_seconds: float = 165.0,
    ) -> dict[str, Any]:
        max_seconds = max(30.0, min(float(max_seconds), 170.0))
        lease_token = uuid4().hex
        deadline = datetime.now(timezone.utc) + timedelta(seconds=max_seconds)
        deadline_text = deadline.replace(microsecond=0).isoformat()
        if not self._claim_run(run_id, lease_token, deadline_text):
            return self.status(run_id)
        try:
            self._run_global_discovery(run_id, lease_token)
            with ThreadPoolExecutor(
                max_workers=self.parallel_channels,
                thread_name_prefix="bergs-mfn-channel",
            ) as executor:
                while self._has_processing_time(deadline):
                    items: list[dict[str, Any]] = []
                    for _ in range(self.parallel_channels):
                        item = self._claim_item(run_id, lease_token)
                        if item is None:
                            break
                        items.append(item)
                    if not items:
                        break
                    futures = {
                        executor.submit(
                            self._process_issuer,
                            int(item["issuer_id"]),
                            force_backfill=bool(item["force_backfill"]),
                            deadline=deadline,
                        ): item
                        for item in items
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            result = future.result()
                            self._finish_item(
                                int(item["id"]),
                                status=str(result["status"]),
                                documents_seen=int(
                                    result.get("documents_seen") or 0
                                ),
                                documents_prepared=int(
                                    result.get("documents_prepared") or 0
                                ),
                                reason=str(result.get("reason") or ""),
                            )
                        except Exception as exc:
                            LOGGER.warning(
                                "report_refresh_issuer_failed "
                                "run=%s issuer=%s error=%s",
                                run_id,
                                item["issuer_id"],
                                exc,
                            )
                            self._finish_item(
                                int(item["id"]),
                                status="failed",
                                error=str(exc),
                            )
            self._finish_slice(run_id, lease_token)
        except Exception as exc:
            self._pause_run(run_id, lease_token, str(exc))
        return self.status(run_id)

    def _active_issuer_ids(self) -> list[int]:
        issuers, _ = self.actions._screening_universe()
        ordered = sorted(
            issuers.items(),
            key=lambda item: (item[1]["name"].casefold(), item[0]),
        )
        return [issuer_id for issuer_id, _ in ordered]

    def _claim_run(
        self, run_id: int, lease_token: str, deadline_at: str
    ) -> bool:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """
                SELECT id FROM report_refresh_runs
                WHERE status='running' AND deadline_at IS NOT NULL
                  AND deadline_at<?
                """,
                (now,),
            ).fetchall()
            for row in stale:
                stale_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE report_refresh_items
                    SET status='pending', updated_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    (now, stale_id),
                )
                connection.execute(
                    """
                    UPDATE report_refresh_runs
                    SET status='paused', lease_token=NULL, deadline_at=NULL,
                        updated_at=?, error='Föregående körning nådde tidsgränsen'
                    WHERE id=? AND status='running'
                    """,
                    (now, stale_id),
                )
            cursor = connection.execute(
                """
                UPDATE report_refresh_runs
                SET status='running', lease_token=?, deadline_at=?,
                    started_at=COALESCE(started_at, ?), updated_at=?,
                    error=NULL
                WHERE id=? AND status IN ('queued', 'paused')
                """,
                (lease_token, deadline_at, now, now, run_id),
            )
            return cursor.rowcount == 1

    def _run_global_discovery(self, run_id: int, lease_token: str) -> None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT global_discovery_status FROM report_refresh_runs
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (run_id, lease_token),
            ).fetchone()
        if not row or row["global_discovery_status"] == "completed":
            return
        try:
            output = self._refresh_global_mfn()
            status = "completed"
            error = None
        except Exception as exc:
            output = None
            status = "failed"
            error = str(exc)
            LOGGER.warning(
                "report_refresh_global_failed run=%s error=%s",
                run_id,
                error,
            )
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE report_refresh_runs
                SET global_discovery_status=?, global_discovery_json=?,
                    updated_at=?, error=?
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    status,
                    json.dumps(output, ensure_ascii=False) if output else None,
                    utc_now(),
                    error,
                    run_id,
                    lease_token,
                ),
            )

    def _refresh_global_mfn(self) -> dict[str, Any]:
        http = HttpClient(
            user_agent=self.settings.user_agent,
            timeout=self.settings.http_timeout,
            min_interval=self.settings.min_request_interval,
            retries=1,
        )
        adapter = MfnDiscoveryAdapter(http)
        run_id = self.db.start_run(
            "mfn", "all_news_discovery", adapter.source_url
        )
        try:
            result = adapter.fetch()
            created = linked = queued = unmatched = 0
            for item in result.items:
                outcome, should_queue = self.db.upsert_document(
                    item, universe_only=True
                )
                if outcome == "unmatched":
                    unmatched += 1
                elif outcome == "created":
                    created += 1
                else:
                    linked += 1
                queued += int(outcome != "unmatched" and should_queue)
            self.db.finish_run(
                run_id,
                status="success",
                http_status=result.status,
                items_seen=len(result.items),
                items_upserted=created,
                items_matched=created + linked,
                items_queued=queued,
                response_sha256=result.response_hash,
            )
            return {
                "seen": len(result.items),
                "created": created,
                "linked": linked,
                "queued": queued,
                "unmatched": unmatched,
            }
        except Exception as exc:
            self.db.finish_run(run_id, status="failed", error=str(exc))
            raise

    def _claim_item(
        self, run_id: int, lease_token: str
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT force_backfill FROM report_refresh_runs
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (run_id, lease_token),
            ).fetchone()
            if not run:
                return None
            item = connection.execute(
                """
                SELECT id, issuer_id FROM report_refresh_items
                WHERE run_id=? AND status='pending'
                ORDER BY id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if not item:
                return None
            cursor = connection.execute(
                """
                UPDATE report_refresh_items
                SET status='running', attempt_count=attempt_count+1,
                    updated_at=?, error=NULL
                WHERE id=? AND status='pending'
                """,
                (now, item["id"]),
            )
            if cursor.rowcount != 1:
                return None
            return {
                "id": int(item["id"]),
                "issuer_id": int(item["issuer_id"]),
                "force_backfill": bool(run["force_backfill"]),
            }

    def _process_issuer(
        self,
        issuer_id: int,
        *,
        force_backfill: bool,
        deadline: datetime | None = None,
    ) -> dict[str, Any]:
        if not force_backfill and self._checked_recently(issuer_id):
            return {
                "status": "skipped_recent",
                "documents_seen": 0,
                "documents_prepared": 0,
                "reason": "MFN-bolagssidan kontrollerades de senaste 24 timmarna",
            }
        try:
            refreshed = self.actions.refresh_mfn(
                issuer_id,
                timeout=self._request_timeout(deadline, requests=2),
                prepare_report=False,
            )
        except MfnIssuerPageEmptyError:
            reason = (
                "Ingen verifierad MFN-bolagssida hittades; nyhetslucka kvarstår"
            )
            self._save_issuer_state(
                issuer_id,
                status="no_report",
                document_id=None,
            )
            return {
                "status": "no_report",
                "documents_seen": 0,
                "documents_prepared": 0,
                "reason": reason,
            }
        prepared_after = prepare_documents(
            self.store,
            self.settings,
            limit=1,
            issuer_id=issuer_id,
            prefer_reports=True,
            request_timeout=self._request_timeout(deadline, requests=3),
        )
        readiness = self.store.latest_report_readiness(issuer_id)
        news_seen = int(refreshed.get("seen") or 0)
        news_stored = int(refreshed.get("created") or 0) + int(
            refreshed.get("linked") or 0
        )
        if readiness and readiness["readiness"] == "complete":
            status = "ready"
            reason = (
                f"{news_stored} MFN-nyheter sparade; komplett "
                "rapportunderlag finns"
            )
            document_id = int(readiness["id"])
        else:
            status = "no_report"
            report_reason = (
                str(readiness["reason"])
                if readiness
                else "Ingen komplett kvartalsrapport hittades på MFN"
            )
            reason = f"{news_stored} MFN-nyheter sparade; {report_reason}"
            document_id = int(readiness["id"]) if readiness else None
        post_report_extracted = 0
        if status == "ready":
            post_report_extracted = self._extract_post_report_documents(
                issuer_id,
                report_document_id=document_id,
                deadline=deadline,
            )
            if post_report_extracted:
                reason += (
                    f"; {post_report_extracted} finansierings-PM efter "
                    "rapportperioden extraherade"
                )
        self._save_issuer_state(
            issuer_id,
            status=status,
            document_id=document_id,
        )
        return {
            "status": status,
            "documents_seen": news_seen,
            "documents_prepared": int(prepared_after["prepared"]),
            "post_report_extracted": post_report_extracted,
            "reason": reason,
        }

    def _extract_post_report_documents(
        self,
        issuer_id: int,
        *,
        report_document_id: int | None,
        deadline: datetime | None,
    ) -> int:
        """Extrahera köade finansierings-PM publicerade EFTER rapportperioden.

        Backfill-flödet extraherar annars bara rapporten (``prefer_reports=
        True, limit=1``). Ett PM om en genomförd emission/lån som publiceras
        efter rapportperiodens slut stannar då som ``metadata_only`` och
        räknas aldrig in i kassan — bolag som redan löst sin finansiering
        flaggas felaktigt för akut kapitalbehov. Detta pass speglar det
        post-rapport-flöde som den manuella omtolkningen kör: förbered texten,
        välj PM efter rapportperioden, kör AI-extraktion per dokument.

        Returnerar antalet PM som skickades till AI-extraktion. Ett fel i
        detta pass får aldrig stoppa huvudflödet — rapporten är redan klar.
        """
        try:
            report_period_end = self.store.latest_report_period_end(issuer_id)
            if not report_period_end:
                return 0
            # Förbered texten för köade PM (inte bara rapporter) FÖRE urvalet.
            # ``post_report_candidates`` kräver ``document_contents.status=
            # 'extracted'`` — utan detta steg extraheras aldrig PM-texten.
            prepare_documents(
                self.store,
                self.settings,
                limit=25,
                issuer_id=issuer_id,
                prefer_reports=False,
                request_timeout=self._request_timeout(deadline, requests=3),
            )
            candidates = self.store.post_report_candidates(
                issuer_id,
                report_period_end,
                exclude_document_id=report_document_id,
            )
            if not candidates:
                return 0
            deadline_at = (
                deadline.isoformat()
                if deadline is not None
                else (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=float(self.settings.http_timeout) * 4)
                ).isoformat()
            )
            analysis = analyze_documents(
                self.store,
                self.settings,
                limit=len(candidates),
                dry_run=False,
                issuer_id=issuer_id,
                candidates=candidates,
                deadline_at=deadline_at,
            )
            return int(analysis.get("seen") or 0)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "post_report_extract_failed issuer=%s error=%s",
                issuer_id,
                exc,
            )
            return 0

    def _request_timeout(
        self,
        deadline: datetime | None,
        *,
        requests: int,
    ) -> float:
        configured = float(self.settings.http_timeout)
        if deadline is None:
            return configured
        remaining = (
            deadline - datetime.now(timezone.utc)
        ).total_seconds() - 12.0
        if remaining <= 3.0:
            raise TimeoutError("MFN-körningens tidsbudget är slut")
        return min(configured, max(3.0, remaining / max(1, requests)))

    def _checked_recently(self, issuer_id: int) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT last_checked_at FROM issuer_report_refresh_state
                WHERE issuer_id=?
                """,
                (issuer_id,),
            ).fetchone()
        if not row or not row["last_checked_at"]:
            return False
        try:
            checked_at = datetime.fromisoformat(str(row["last_checked_at"]))
        except ValueError:
            return False
        return checked_at >= datetime.now(timezone.utc) - timedelta(hours=24)

    def _save_issuer_state(
        self,
        issuer_id: int,
        *,
        status: str,
        document_id: int | None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO issuer_report_refresh_state (
                    issuer_id, last_checked_at, last_status,
                    last_report_document_id, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id) DO UPDATE SET
                    last_checked_at=excluded.last_checked_at,
                    last_status=excluded.last_status,
                    last_report_document_id=excluded.last_report_document_id,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (issuer_id, now, status, document_id, error, now),
            )

    def _finish_item(
        self,
        item_id: int,
        *,
        status: str,
        documents_seen: int = 0,
        documents_prepared: int = 0,
        reason: str = "",
        error: str | None = None,
    ) -> None:
        if status not in TERMINAL_ITEM_STATUSES:
            raise ValueError(status)
        now = utc_now()
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT issuer_id FROM report_refresh_items WHERE id=?
                """,
                (item_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE report_refresh_items
                SET status=?, documents_seen=?, documents_prepared=?,
                    reason=?, error=?, completed_at=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    documents_seen,
                    documents_prepared,
                    reason[:4000],
                    error[:4000] if error else None,
                    now,
                    now,
                    item_id,
                ),
            )
        if status == "failed" and row:
            self._save_issuer_state(
                int(row["issuer_id"]),
                status="failed",
                document_id=None,
                error=error,
            )

    def _finish_slice(self, run_id: int, lease_token: str) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM report_refresh_items
                    WHERE run_id=? AND status IN ('pending', 'running')
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            if pending:
                connection.execute(
                    """
                    UPDATE report_refresh_items
                    SET status='pending', updated_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    (now, run_id),
                )
                status = "paused"
                completed_at = None
            else:
                status = "completed"
                completed_at = now
            connection.execute(
                """
                UPDATE report_refresh_runs
                SET status=?, lease_token=NULL, deadline_at=NULL,
                    updated_at=?, completed_at=?, error=NULL
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    status,
                    now,
                    completed_at,
                    run_id,
                    lease_token,
                ),
            )

    def _pause_run(
        self, run_id: int, lease_token: str, error: str
    ) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE report_refresh_items
                SET status='pending', updated_at=?
                WHERE run_id=? AND status='running'
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE report_refresh_runs
                SET status='paused', lease_token=NULL, deadline_at=NULL,
                    updated_at=?, error=?
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (now, error[:4000], run_id, lease_token),
            )

    def _has_processing_time(self, deadline: datetime) -> bool:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        network_margin = min(
            65.0,
            max(20.0, (self.settings.http_timeout * 2.0) + 5.0),
        )
        return remaining > network_margin


class ReportRefreshWorker:
    def __init__(
        self,
        service: ReportRefreshService,
        *,
        max_seconds: float = 165.0,
    ) -> None:
        self.service = service
        self.max_seconds = max_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(
        self,
        *,
        trigger_kind: str,
        force_backfill: bool,
    ) -> dict[str, Any]:
        run = self.service.start_or_resume(
            trigger_kind=trigger_kind,
            force_backfill=force_backfill,
        )
        self._launch(int(run["id"]))
        return run

    def resume(self) -> None:
        run = self.service.latest()
        # A paused slice is intentionally dormant until a manual action calls
        # start(). Dashboard startup must not turn a
        # paused, token-free collection into an implicit network run.
        if run and run["status"] == "queued":
            self._launch(int(run["id"]))

    def status(self) -> dict[str, Any] | None:
        return self.service.latest()

    def _launch(self, run_id: int) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id,),
                name="bergs-report-refresh",
                daemon=True,
            )
            self._thread.start()

    def _run(self, run_id: int) -> None:
        started = time.monotonic()
        # En manuellt startad körning ska köras klart utan manuella ticks.
        # Varje slice pausar när tidsbudgeten tar slut; loopa därför slices
        # tills körningen når "completed" (eller inte längre kan claimas).
        # Detta påverkar inte resume() vid serverstart, som avsiktligt lämnar
        # pausade körningar orörda.
        while True:
            slice_started = time.monotonic()
            result = self.service.run_slice(
                run_id,
                max_seconds=self.max_seconds,
            )
            LOGGER.info(
                "report_refresh_slice run=%s status=%s finished=%s total=%s "
                "duration_seconds=%.3f",
                run_id,
                result["status"],
                result["progress"]["finished"],
                result["progress"]["total"],
                time.monotonic() - slice_started,
            )
            if result["status"] != "paused":
                break
        LOGGER.info(
            "report_refresh_done run=%s status=%s finished=%s total=%s "
            "duration_seconds=%.3f",
            run_id,
            result["status"],
            result["progress"]["finished"],
            result["progress"]["total"],
            time.monotonic() - started,
        )
