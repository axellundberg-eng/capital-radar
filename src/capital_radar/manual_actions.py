from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .analysis_store import AnalysisStore
from .config import Settings
from .db import Database
from .extraction import analyze_documents
from .http import HttpClient
from .mfn_identity import known_mfn_slug
from .normalization import utc_now
from .runway import compute_runways
from .screening_eligibility import classify_screening_eligibility
from .services.documents import prepare_documents
from .sources.mfn import BASE_URL, MfnDiscoveryAdapter


LOGGER = logging.getLogger("bergs.ai")

PROGRESS_MESSAGES = {
    "queued": "Väntar i AI-kön",
    "refreshing": "Hämtar senaste MFN-underlaget",
    "selecting_report": "Väljer starkaste kompletta rapport",
    "preparing_text": "Förbereder källtext för analys",
    "calling_kimi": "AI analyserar rapporten",
    "validating": "Validerar AI-svar och källhänvisningar",
    "computing_runway": "Beräknar runway och kapitalbehov",
    "web_fallback": "Söker externa rapportkällor när fakta saknas",
    "awaiting_sources": "Inväntar källunderlag för automatisk omanalys",
    "saving": "Sparar analys och spårbar historik",
    "completed": "Analysen är klar",
    "insufficient_facts": "Underlaget räcker inte för en säker beräkning",
    "failed": "Analysen misslyckades",
    "timed_out": "Analysen avbröts vid tidsgränsen",
    "cancelled": "Analysen avbröts manuellt",
}


class ActionTimedOut(RuntimeError):
    pass


class ActionConflictError(RuntimeError):
    """Kastas nar en action inte kan avbrytas/andras i nuvarande status."""


class ManualActionService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.store = AnalysisStore(db)
        self.recover_expired_actions()

    def set_watch(self, issuer_id: int, active: bool) -> dict[str, Any]:
        issuer = self._issuer(issuer_id)
        self.store.set_watchlist(issuer_id, active=active)
        return {
            "issuer_id": issuer_id,
            "issuer": issuer["canonical_name"],
            "watching": active,
        }

    def refresh_mfn(
        self,
        issuer_id: int,
        *,
        limit: int = 50,
        timeout: float | None = None,
        prepare_report: bool = True,
    ) -> dict[str, Any]:
        issuer = self._issuer(issuer_id)
        action_id = self._start_action(issuer_id, "refresh_mfn")
        source_run_id: int | None = None
        try:
            issuer_slug, issuer_url = self._resolve_mfn_identity(
                issuer_id, issuer["canonical_name"]
            )
            http = HttpClient(
                user_agent=self.settings.user_agent,
                timeout=(
                    self.settings.http_timeout
                    if timeout is None
                    else max(3.0, min(timeout, self.settings.http_timeout))
                ),
                min_interval=self.settings.min_request_interval,
                retries=1,
            )
            adapter = MfnDiscoveryAdapter(http)
            source_run_id = self.db.start_run(
                "mfn", "issuer_manual_refresh", issuer_url
            )
            result = adapter.fetch_issuer(
                issuer_url,
                issuer_name=issuer["canonical_name"],
                issuer_slug=issuer_slug,
                limit=limit,
            )
            created = linked = queued = 0
            for source_item in result.items:
                item = replace(
                    source_item,
                    issuer_name=issuer["canonical_name"],
                    metadata={
                        **source_item.metadata,
                        "source_issuer_name": source_item.issuer_name,
                        "collection_policy": "store_all_then_classify",
                    },
                )
                outcome, should_queue = self.db.upsert_document(
                    item, universe_only=True
                )
                if outcome == "created":
                    created += 1
                elif outcome == "linked":
                    linked += 1
                queued += int(outcome != "unmatched" and should_queue)
            self.db.finish_run(
                source_run_id,
                status="success",
                http_status=result.status,
                items_seen=len(result.items),
                items_upserted=created,
                items_matched=created + linked,
                items_queued=queued,
                response_sha256=result.response_hash,
            )
            self.store.set_watchlist(
                issuer_id, active=True, mfn_refreshed=True
            )
            prepared = None
            readiness = None
            if prepare_report:
                prepared = prepare_documents(
                    self.store,
                    self.settings,
                    limit=1,
                    issuer_id=issuer_id,
                    prefer_reports=True,
                    request_timeout=timeout,
                )
                readiness = self.store.latest_report_readiness(issuer_id)
            output = {
                "issuer_id": issuer_id,
                "issuer": issuer["canonical_name"],
                "mfn_url": issuer_url,
                "seen": len(result.items),
                "created": created,
                "linked": linked,
                "queued": queued,
                "watching": True,
                "report_preparation": prepared,
                "report_readiness": readiness,
            }
            self._finish_action(action_id, output)
            return output
        except Exception as exc:
            if source_run_id is not None:
                self.db.finish_run(
                    source_run_id, status="failed", error=str(exc)
                )
            self._fail_action(action_id, str(exc))
            raise

    def enqueue_interpret(
        self, issuer_id: int, *, trigger_kind: str = "manual"
    ) -> dict[str, Any]:
        if trigger_kind not in {"manual", "automation"}:
            raise ValueError("Unknown analysis trigger")
        if trigger_kind == "automation" and not self.settings.auto_analysis_enabled:
            raise ValueError("Automatic analysis is disabled")
        issuer = self._issuer(issuer_id)
        self.recover_expired_actions()
        now = utc_now()
        active_action_id: int | None = None
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT id FROM manual_action_runs
                WHERE issuer_id=? AND action_type='interpret_capital_need'
                  AND status IN ('queued', 'running')
                ORDER BY id DESC LIMIT 1
                """,
                (issuer_id,),
            ).fetchone()
            if active:
                active_action_id = int(active["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO manual_action_runs (
                        issuer_id, action_type, status, phase, deadline_at,
                        started_at, updated_at
                    ) VALUES (
                        ?, 'interpret_capital_need', 'queued', 'queued',
                        NULL, ?, ?
                    )
                    """,
                    (issuer_id, now, now),
                )
                action_id = int(cursor.lastrowid)
                self._append_progress_event(
                    connection,
                    action_id=action_id,
                    phase="queued",
                    status="queued",
                    metadata={"trigger_kind": trigger_kind},
                    created_at=now,
                )
        if active_action_id is not None:
            payload = self.action_status(active_action_id)
            payload["created"] = False
            return payload
        payload = self.action_status(action_id)
        payload["created"] = True
        return payload

    def enqueue_force_reinterpret(
        self, issuer_id: int, *, idempotency_key: str, estimated_tokens: int,
        action_type: str = "force_reinterpret"
    ) -> dict[str, Any]:
        """Queue a source-refreshing interpretation with durable idempotency."""
        issuer = self._issuer(issuer_id)
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS force_reinterpret_requests(
                    idempotency_key TEXT PRIMARY KEY,
                    action_id INTEGER NOT NULL,
                    issuer_id INTEGER NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            existing = connection.execute(
                "SELECT action_id FROM force_reinterpret_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                payload = self.action_status(int(existing["action_id"]))
                payload["created"] = False
                payload["idempotent"] = True
                return payload
            cursor = connection.execute(
                """INSERT INTO manual_action_runs
                   (issuer_id, action_type, status, phase, started_at, updated_at)
                   VALUES (?, ?, 'queued', 'queued', ?, ?)""",
                (issuer_id, action_type, now, now),
            )
            action_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO force_reinterpret_requests
                   (idempotency_key, action_id, issuer_id, estimated_tokens, created_at)
                   VALUES (?,?,?,?,?)""",
                (idempotency_key, action_id, issuer_id, estimated_tokens, now),
            )
            self._append_progress_event(
                connection, action_id=action_id, phase="queued", status="queued",
                metadata={"force_reinterpret": True, "estimated_tokens": estimated_tokens},
                created_at=now,
            )
        payload = self.action_status(action_id)
        payload["created"] = True
        payload["idempotent"] = False
        payload["estimated_tokens"] = estimated_tokens
        return payload

    def select_analysis_candidates(
        self, limit: int, *, max_attempts: int = 3, cooldown_hours: int = 24
    ) -> list[int]:
        return [
            item["issuer_id"]
            for item in self.list_analysis_candidates(
                limit, max_attempts=max_attempts, cooldown_hours=cooldown_hours
            )
        ]

    def list_analysis_candidates(
        self, limit: int, *, max_attempts: int = 3, cooldown_hours: int = 24
    ) -> list[dict]:
        """Som select_analysis_candidates men returnerar rika poster.

        Avsett för GET /api/analysis/candidates så att en extern agent
        själv kan driva analysloopen: den ser vilka bolag som väntar,
        varför (reason) och i vilken prioritetsordning.

        reason:
          - "new_report"      : nytt kassasignal-dokument sedan senaste analys
          - "runway_rule_upgrade": analys med en ersatt runway-regel
          - "never_analyzed"  : aldrig analyserat (eller ej completed)
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                WITH latest_doc AS (
                    SELECT issuer_id, datetime(MAX(MAX(
                        COALESCE(julianday(published_at), 0),
                        COALESCE(julianday(created_at), 0)
                    ))) AS latest_published
                    FROM documents
                    WHERE cash_relevance_score > 0
                       OR profile_category = 'report'
                       OR analysis_status IN ('extracted', 'content_ready')
                    GROUP BY issuer_id
                ),
                fail_counts AS (
                    SELECT issuer_id,
                           COUNT(*) AS fail_count,
                           MAX(julianday(COALESCE(completed_at, started_at))) AS last_fail_at
                    FROM manual_action_runs
                    WHERE action_type='interpret_capital_need'
                      AND status IN ('failed', 'timed_out', 'insufficient_facts')
                      AND julianday(COALESCE(completed_at, started_at))
                          > julianday('now', ?)
                    GROUP BY issuer_id
                ),
                active AS (
                    SELECT DISTINCT issuer_id
                    FROM manual_action_runs
                    WHERE action_type='interpret_capital_need'
                      AND status IN ('queued', 'running')
                )
                SELECT
                    i.id,
                    i.canonical_name,
                    ld.latest_published,
                    s.last_attempt_at,
                    s.analysis_status,
                    CASE
                        WHEN ld.latest_published IS NOT NULL
                             AND (s.last_attempt_at IS NULL
                                  OR julianday(ld.latest_published) > julianday(s.last_attempt_at))
                        THEN 0
                        WHEN r.calculation_json LIKE '%"version": "runway-v1"%'
                        THEN 1
                        ELSE 2
                    END AS prio
                FROM issuers i
                LEFT JOIN issuer_analysis_summary s ON s.issuer_id = i.id
                LEFT JOIN latest_doc ld ON ld.issuer_id = i.id
                LEFT JOIN fail_counts fc ON fc.issuer_id = i.id
                LEFT JOIN runway_assessments r
                  ON r.id=s.current_runway_assessment_id
                WHERE i.id NOT IN (SELECT issuer_id FROM active)
                  AND (
                    s.issuer_id IS NULL
                    OR s.analysis_status != 'completed'
                    OR (SELECT status FROM manual_action_runs
                        WHERE issuer_id=i.id AND action_type='interpret_capital_need'
                        ORDER BY id DESC LIMIT 1) IN (
                            'failed', 'timed_out', 'insufficient_facts'
                        )
                    OR (ld.latest_published IS NOT NULL
                        AND julianday(ld.latest_published) > julianday(s.last_attempt_at))
                    OR r.calculation_json LIKE '%"version": "runway-v1"%'
                  )
                  AND COALESCE(fc.fail_count, 0) < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM manual_action_runs recent
                    WHERE recent.issuer_id=i.id
                      AND recent.action_type='interpret_capital_need'
                      AND julianday(COALESCE(recent.completed_at, recent.started_at))
                          > julianday('now', '-10 minutes')
                  )
                  AND NOT (
                    s.last_attempt_at IS NOT NULL
                    AND julianday(s.last_attempt_at) > julianday('now', '-10 minutes')
                  )
                ORDER BY prio ASC,
                         ld.latest_published DESC NULLS LAST,
                         i.canonical_name ASC
                LIMIT ?
                """,
                (f"-{cooldown_hours} hours", max_attempts, max(0, limit)),
            ).fetchall()

        candidates: list[dict] = []
        for row in rows:
            reason = (
                "new_report" if row["prio"] == 0
                else "runway_rule_upgrade" if row["prio"] == 1
                else "retry" if row["last_attempt_at"] is not None
                else "never_analyzed"
            )
            candidates.append(
                {
                    "issuer_id": int(row["id"]),
                    "canonical_name": row["canonical_name"],
                    "reason": reason,
                    "priority": int(row["prio"]),
                    "latest_document_at": row["latest_published"],
                    "last_attempt_at": row["last_attempt_at"],
                    "analysis_status": row["analysis_status"],
                }
            )
        return candidates

    def count_active_interpret_actions(self) -> int:
        """Antal aktiva (queued/running) interpret-actions just nu.

        Används av auto-analysens topp-upp-logik för att hålla kön
        konstant fylld mot ett mål utan att skapa dubbletter.
        """
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n
                FROM manual_action_runs
                WHERE action_type='interpret_capital_need'
                  AND status IN ('queued', 'running')
                """
            ).fetchone()
        return int(row["n"] if row else 0)

    def auto_analysis_status(
        self, *, max_attempts: int = 3, cooldown_hours: int = 24
    ) -> dict[str, Any]:
        """Read-only diagnostik för den automatiska analys-schedulern.

        Visar exakt vad schedulern ser: hur många bolag som är kandidater
        uppdelat på orsak (nya rapporter / aldrig analyserade), hur många
        som blockeras av aktiv action eller retry-gräns, samt de senaste
        interpret-actionsen. Avslöjar inga hemligheter.
        """
        with self.db.connect() as connection:
            counts = connection.execute(
                """
                WITH failures AS (
                    SELECT issuer_id FROM manual_action_runs
                    WHERE action_type='interpret_capital_need'
                      AND status IN ('failed', 'timed_out', 'insufficient_facts')
                      AND julianday(COALESCE(completed_at, started_at))
                          > julianday('now', ?)
                    GROUP BY issuer_id HAVING COUNT(*) >= ?
                ), active AS (
                    SELECT DISTINCT issuer_id FROM manual_action_runs
                    WHERE action_type='interpret_capital_need'
                      AND status IN ('queued', 'running')
                )
                SELECT (SELECT COUNT(*) FROM active) AS blocked_active,
                       (SELECT COUNT(*) FROM failures
                        WHERE issuer_id NOT IN (SELECT issuer_id FROM active))
                           AS blocked_retry,
                       (SELECT COUNT(*) FROM issuer_analysis_summary s
                        WHERE s.analysis_status='completed'
                          AND s.issuer_id NOT IN (SELECT issuer_id FROM active)
                          AND COALESCE((
                              SELECT status FROM manual_action_runs
                              WHERE issuer_id=s.issuer_id
                                AND action_type='interpret_capital_need'
                              ORDER BY id DESC LIMIT 1
                          ), 'completed') NOT IN (
                              'failed', 'timed_out', 'insufficient_facts'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM documents d
                              WHERE d.issuer_id=s.issuer_id
                                AND (d.cash_relevance_score > 0
                                     OR d.profile_category='report'
                                     OR d.analysis_status IN ('extracted', 'content_ready'))
                                AND (s.last_attempt_at IS NULL
                                     OR julianday(d.published_at) > julianday(s.last_attempt_at)
                                     OR julianday(d.created_at) > julianday(s.last_attempt_at))
                          )) AS up_to_date
                """,
                (f"-{cooldown_hours} hours", max_attempts),
            ).fetchone()
            recent = connection.execute(
                """
                SELECT a.id, a.issuer_id, i.canonical_name AS issuer,
                       a.status, a.phase, a.started_at, a.updated_at
                FROM manual_action_runs a
                JOIN issuers i ON i.id = a.issuer_id
                WHERE a.action_type='interpret_capital_need'
                ORDER BY a.id DESC
                LIMIT 15
                """
            ).fetchall()
        row = dict(counts) if counts else {}
        candidates = self.list_analysis_candidates(
            2147483647, max_attempts=max_attempts, cooldown_hours=cooldown_hours
        )
        queue_target = int(
            getattr(self.settings, "auto_analysis_queue_target", 20)
        )
        return {
            "candidates_new_reports": sum(c["reason"] == "new_report" for c in candidates),
            "candidates_never_analyzed": sum(c["reason"] == "never_analyzed" for c in candidates),
            "candidates_retry": sum(c["reason"] == "retry" for c in candidates),
            "candidate_count": len(candidates),
            "blocked_active_action": int(row.get("blocked_active") or 0),
            "blocked_retry_limit": int(row.get("blocked_retry") or 0),
            "up_to_date": int(row.get("up_to_date") or 0),
            "active_count": self.count_active_interpret_actions(),
            "queue_target": queue_target,
            "recent_actions": [dict(r) for r in recent],
        }

    def action_status(self, action_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT a.id, a.issuer_id, i.canonical_name AS issuer,
                       a.action_type, a.status, a.phase, a.deadline_at,
                       a.started_at, a.updated_at, a.completed_at,
                       a.result_json, a.error
                FROM manual_action_runs a
                JOIN issuers i ON i.id=a.issuer_id
                WHERE a.id=?
                """,
                (action_id,),
            ).fetchone()
            if not row:
                raise KeyError(action_id)
            event_rows = connection.execute(
                """
                SELECT id, sequence_no, phase, status, message, document_id,
                       extraction_job_id, estimated_tokens, actual_tokens,
                       metadata_json, created_at
                FROM action_progress_events
                WHERE action_id=?
                ORDER BY sequence_no
                """,
                (action_id,),
            ).fetchall()
            queued_total = 0
            queue_position: int | None = None
            if (
                str(row["action_type"]) == "interpret_capital_need"
                and str(row["status"]) == "queued"
            ):
                queued_total = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM manual_action_runs
                        WHERE action_type='interpret_capital_need'
                          AND status='queued'
                        """
                    ).fetchone()[0]
                )
                ahead = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM manual_action_runs
                        WHERE action_type='interpret_capital_need'
                          AND (
                              status='running'
                              OR (status='queued' AND id<?)
                          )
                        """,
                        (action_id,),
                    ).fetchone()[0]
                )
                queue_position = ahead + 1
            elif (
                str(row["action_type"]) == "interpret_capital_need"
                and str(row["status"]) == "running"
            ):
                queued_total = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM manual_action_runs
                        WHERE action_type='interpret_capital_need'
                          AND status='queued'
                        """
                    ).fetchone()[0]
                )
                queue_position = 0
        payload = dict(row)
        result_json = payload.pop("result_json")
        payload["result"] = (
            json.loads(result_json) if result_json else None
        )
        events: list[dict[str, Any]] = []
        selected_document: dict[str, Any] | None = None
        estimated_tokens: int | None = None
        actual_tokens: int | None = None
        for event_row in event_rows:
            event = dict(event_row)
            metadata = _load_json_dict(event.pop("metadata_json"))
            event["metadata"] = metadata
            events.append(event)
            if event["document_id"] is not None:
                selected_document = {
                    "document_id": int(event["document_id"]),
                    "title": metadata.get("document_title"),
                    "source_url": metadata.get("source_url"),
                    "extraction_job_id": event["extraction_job_id"],
                }
            if event["estimated_tokens"] is not None:
                estimated_tokens = int(event["estimated_tokens"])
            if event["actual_tokens"] is not None:
                actual_tokens = int(event["actual_tokens"])
        progress = {
            "phase": payload["phase"],
            "message": PROGRESS_MESSAGES.get(
                str(payload["phase"]), str(payload["phase"])
            ),
            "queue_position": queue_position,
            "queued_total": queued_total,
            "deadline_at": payload["deadline_at"],
            "selected_document": selected_document,
            "estimated_input_tokens": estimated_tokens,
            "actual_tokens": actual_tokens,
            "events": events,
        }
        payload["progress"] = progress
        payload["queue_position"] = queue_position
        payload["selected_document"] = selected_document
        payload["estimated_input_tokens"] = estimated_tokens
        payload["actual_tokens"] = actual_tokens
        return payload

    def cancel_action(
        self,
        action_id: int,
        *,
        actor: str = "api-user",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Avbryt en queued/running action manuellt.

        Satter status 'cancelled', rensar lease_token och stanger posten.
        Bara actions som annu inte natt ett terminalt tillstand kan
        avbrytas — annars kastas AnalysisUpdateConflict (409).
        Returnerar den uppdaterade action-posten.
        """
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status FROM manual_action_runs
                WHERE id=?
                """,
                (action_id,),
            ).fetchone()
            if not row:
                raise KeyError(action_id)
            if str(row["status"]) not in ("queued", "running"):
                raise ActionConflictError(
                    f"Åtgärden kan inte avbrytas i status "
                    f"'{row['status']}' — bara queued/running kan avbrytas"
                )
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='cancelled', phase='cancelled', lease_token=NULL,
                    completed_at=?, updated_at=?,
                    error='Åtgärden avbröts manuellt'
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (now, now, action_id),
            )
            if cursor.rowcount != 1:
                raise ActionConflictError(
                    "Åtgärden hann ändra status innan den kunde avbrytas"
                )
            self._append_progress_event(
                connection,
                action_id=action_id,
                phase="cancelled",
                status="cancelled",
                message="Åtgärden avbröts manuellt",
                created_at=now,
            )
        return self.action_status(action_id)

    def pending_action_ids(self) -> list[int]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM manual_action_runs
                WHERE (action_type='interpret_capital_need' OR action_type LIKE 'force_reinterpret%')
                  AND status='queued'
                ORDER BY id
                """
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def screening_filter_preview(self, stage: str) -> dict[str, Any]:
        stage_order = ("company_type", "foreign", "report", "ai_call")
        if stage not in stage_order:
            raise ValueError(f"Okänt filtersteg: {stage}")
        issuers, overrides = self._screening_universe()

        company_type_remaining: list[int] = []
        company_type_removed: list[dict[str, Any]] = []
        for issuer_id, issuer in issuers.items():
            override = overrides.get(issuer_id)
            result = classify_screening_eligibility(
                issuer_name=issuer["name"],
                jurisdiction_code="SE",
                sectors=tuple(sorted(issuer["sectors"])),
                override=override[0] if override else None,
                override_reason=override[1] if override else None,
            )
            if result.eligible:
                company_type_remaining.append(issuer_id)
            else:
                company_type_removed.append(
                    {
                        "issuer_id": issuer_id,
                        "issuer": issuer["name"],
                        "code": result.code,
                        "reason": result.reason,
                    }
                )

        foreign_remaining: list[int] = []
        foreign_removed: list[dict[str, Any]] = []
        for issuer_id in company_type_remaining:
            issuer = issuers[issuer_id]
            override = overrides.get(issuer_id)
            result = classify_screening_eligibility(
                issuer_name=issuer["name"],
                jurisdiction_code=issuer["jurisdiction_code"],
                sectors=tuple(sorted(issuer["sectors"])),
                override=override[0] if override else None,
                override_reason=override[1] if override else None,
            )
            if result.eligible:
                foreign_remaining.append(issuer_id)
            else:
                foreign_removed.append(
                    {
                        "issuer_id": issuer_id,
                        "issuer": issuer["name"],
                        "code": result.code,
                        "reason": result.reason,
                    }
                )

        report_states = self._latest_report_states(foreign_remaining)
        report_remaining = [
            issuer_id
            for issuer_id in foreign_remaining
            if report_states[issuer_id]["complete"]
        ]
        report_removed = [
            {
                "issuer_id": issuer_id,
                "issuer": issuers[issuer_id]["name"],
                "code": report_states[issuer_id]["code"],
                "reason": report_states[issuer_id]["reason"],
            }
            for issuer_id in foreign_remaining
            if not report_states[issuer_id]["complete"]
        ]
        cached = [
            issuer_id
            for issuer_id in report_remaining
            if report_states[issuer_id]["cached"]
        ]
        new_ai = [
            issuer_id
            for issuer_id in report_remaining
            if not report_states[issuer_id]["cached"]
        ]

        all_stages = [
            {
                "id": "company_type",
                "label": "Bolagstyp och finansiell komplexitet",
                "input": len(issuers),
                "removed": len(company_type_removed),
                "remaining": len(company_type_remaining),
                "removed_items": company_type_removed,
            },
            {
                "id": "foreign",
                "label": "Utländska bolag utanför Medtech",
                "input": len(company_type_remaining),
                "removed": len(foreign_removed),
                "remaining": len(foreign_remaining),
                "removed_items": foreign_removed,
            },
            {
                "id": "report",
                "label": "Komplett lokalt rapportunderlag",
                "input": len(foreign_remaining),
                "removed": len(report_removed),
                "remaining": len(report_remaining),
                "removed_items": report_removed,
            },
            {
                "id": "ai_call",
                "label": "Nytt AI-anrop behövs",
                "input": len(report_remaining),
                "removed": len(cached),
                "remaining": len(new_ai),
                "removed_label": "återanvänds från cache",
                "removed_items": [],
            },
        ]
        last_index = stage_order.index(stage)
        return {
            "stage": stage,
            "universe": len(issuers),
            "stages": all_stages[: last_index + 1],
            "relevant": len(foreign_remaining),
            "complete_reports": len(report_remaining),
            "cached_companies": len(cached),
            "projected_new_ai_calls": len(new_ai),
            "generated_at": utc_now(),
        }

    def start_screening_batch(
        self,
        *,
        refresh_mfn: bool = True,
        max_api_calls: int = 10,
        prepared_only: bool = True,
    ) -> dict[str, Any]:
        if max_api_calls < 1 or max_api_calls > 50:
            raise ValueError("API-taket måste vara mellan 1 och 50.")
        now = utc_now()
        issuers, overrides = self._screening_universe()
        if not issuers:
            raise RuntimeError("Det aktiva bolagsuniversumet är tomt.")

        preliminary: list[
            tuple[int, str, int, str | None, str | None]
        ] = []
        for issuer_id, issuer in issuers.items():
            override = overrides.get(issuer_id)
            eligibility = classify_screening_eligibility(
                issuer_name=issuer["name"],
                jurisdiction_code=issuer["jurisdiction_code"],
                sectors=tuple(sorted(issuer["sectors"])),
                override=override[0] if override else None,
                override_reason=override[1] if override else None,
            )
            preliminary.append(
                (
                    issuer_id,
                    "pending" if eligibility.eligible else "skipped",
                    eligibility.priority,
                    None if eligibility.eligible else "not_relevant",
                    eligibility.reason,
                )
            )
        report_states = self._latest_report_states(
            [
                issuer_id
                for issuer_id, status, _, _, _ in preliminary
                if status == "pending"
            ]
        )
        classified: list[
            tuple[int, str, int, str | None, str | None]
        ] = []
        for issuer_id, status, priority, decision, reason in preliminary:
            if (
                prepared_only
                and status == "pending"
                and not report_states[issuer_id]["complete"]
            ):
                classified.append(
                    (
                        issuer_id,
                        "deferred",
                        priority,
                        "unknown",
                        (
                            "Uppskjuten av rapportfiltret: "
                            f"{report_states[issuer_id]['reason']}"
                        ),
                    )
                )
            else:
                classified.append(
                    (issuer_id, status, priority, decision, reason)
                )

        batch_id: int
        created = False
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT id FROM screening_batches
                WHERE status IN ('queued', 'running', 'stopping')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if active:
                batch_id = int(active["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO screening_batches (
                        status, refresh_mfn, prepared_only, max_api_calls,
                        api_calls_used, stop_requested, total_count,
                        created_at, started_at, updated_at
                    ) VALUES ('running', ?, ?, ?, 0, 0, ?, ?, ?, ?)
                    """,
                    (
                        int(refresh_mfn),
                        int(prepared_only),
                        max_api_calls,
                        len(classified),
                        now,
                        now,
                        now,
                    ),
                )
                batch_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO screening_batch_items (
                        batch_id, issuer_id, status, priority, decision,
                        reason, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            batch_id,
                            issuer_id,
                            status,
                            priority,
                            decision,
                            reason,
                            now,
                            now,
                            (
                                now
                                if status in {"skipped", "deferred"}
                                else None
                            ),
                        )
                        for (
                            issuer_id,
                            status,
                            priority,
                            decision,
                            reason,
                        ) in classified
                    ),
                )
                created = True
        next_action_id = self._activate_next_batch_item(batch_id)
        payload = self.screening_batch_status(batch_id)
        payload["created"] = created
        payload["_next_action_id"] = next_action_id
        return payload

    def latest_screening_batch(self) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM screening_batches ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self.screening_batch_status(int(row["id"])) if row else None

    def screening_batch_status(self, batch_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            batch = connection.execute(
                "SELECT * FROM screening_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise KeyError(batch_id)
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)
                        AS pending,
                    SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END)
                        AS queued,
                    SUM(CASE WHEN status='running' THEN 1 ELSE 0 END)
                        AS running,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                        AS completed,
                    SUM(CASE WHEN status='insufficient_facts' THEN 1 ELSE 0 END)
                        AS insufficient_facts,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)
                        AS failed,
                    SUM(CASE WHEN status='timed_out' THEN 1 ELSE 0 END)
                        AS timed_out,
                    SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END)
                        AS cancelled,
                    SUM(CASE WHEN status='deferred' THEN 1 ELSE 0 END)
                        AS deferred,
                    SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END)
                        AS skipped,
                    SUM(CASE WHEN decision='yes' THEN 1 ELSE 0 END)
                        AS capital_need,
                    SUM(CASE WHEN decision='no' THEN 1 ELSE 0 END)
                        AS no_capital_need,
                    SUM(CASE WHEN decision='unknown' THEN 1 ELSE 0 END)
                        AS unknown
                FROM screening_batch_items WHERE batch_id=?
                """,
                (batch_id,),
            ).fetchone()
            current = connection.execute(
                """
                SELECT bi.id, bi.issuer_id, i.canonical_name AS issuer,
                       bi.status, a.id AS action_id, a.phase,
                       a.deadline_at, a.updated_at
                FROM screening_batch_items bi
                JOIN issuers i ON i.id=bi.issuer_id
                LEFT JOIN manual_action_runs a ON a.id=bi.action_id
                WHERE bi.batch_id=? AND bi.status IN ('queued', 'running')
                ORDER BY bi.id LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            recent = connection.execute(
                """
                SELECT bi.id, bi.issuer_id, i.canonical_name AS issuer,
                       bi.status, bi.decision, bi.need_score,
                       bi.need_timing, bi.runway_months, bi.reason,
                       bi.updated_at
                FROM screening_batch_items bi
                JOIN issuers i ON i.id=bi.issuer_id
                WHERE bi.batch_id=? AND bi.status!='pending'
                ORDER BY bi.updated_at DESC, bi.id DESC
                LIMIT 600
                """,
                (batch_id,),
            ).fetchall()
        payload = dict(batch)
        progress = {key: int(value or 0) for key, value in dict(counts).items()}
        active = progress["pending"] + progress["queued"] + progress["running"]
        progress["finished"] = progress["total"] - active
        payload["progress"] = progress
        payload["current"] = dict(current) if current else None
        payload["recent_items"] = [dict(row) for row in recent]
        return payload

    def stop_screening_batch(self, batch_id: int) -> dict[str, Any]:
        now = utc_now()
        already_terminal = False
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT status FROM screening_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise KeyError(batch_id)
            if str(batch["status"]) not in {"queued", "running", "stopping"}:
                already_terminal = True
            if not already_terminal:
                cancelled_action_ids = [
                    int(row["action_id"])
                    for row in connection.execute(
                        """
                        SELECT action_id FROM screening_batch_items
                        WHERE batch_id=? AND status='queued'
                          AND action_id IS NOT NULL
                        ORDER BY id
                        """,
                        (batch_id,),
                    ).fetchall()
                ]
                connection.execute(
                """
                UPDATE screening_batches
                SET stop_requested=1, status='stopping', updated_at=?
                WHERE id=?
                """,
                (now, batch_id),
                )
                connection.execute(
                """
                UPDATE manual_action_runs
                SET status='cancelled', phase='cancelled',
                    completed_at=?, updated_at=?,
                    error='Batchscreeningen stoppades manuellt'
                WHERE id IN (
                    SELECT action_id FROM screening_batch_items
                    WHERE batch_id=? AND status='queued'
                ) AND status='queued'
                """,
                (now, now, batch_id),
                )
                for action_id in cancelled_action_ids:
                    self._append_progress_event(
                        connection,
                        action_id=action_id,
                        phase="cancelled",
                        status="cancelled",
                        message="Batchscreeningen stoppades manuellt",
                        metadata={"batch_id": batch_id},
                        created_at=now,
                    )
                connection.execute(
                """
                UPDATE screening_batch_items
                SET status='cancelled', decision='unknown',
                    reason='Batchscreeningen stoppades manuellt',
                    completed_at=?, updated_at=?
                WHERE batch_id=? AND status IN ('pending', 'queued')
                """,
                (now, now, batch_id),
                )
                running = int(
                    connection.execute(
                    """
                    SELECT COUNT(*) FROM screening_batch_items
                    WHERE batch_id=? AND status='running'
                    """,
                    (batch_id,),
                    ).fetchone()[0]
                )
                if running == 0:
                    connection.execute(
                    """
                    UPDATE screening_batches
                    SET status='stopped', completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, batch_id),
                    )
        return self.screening_batch_status(batch_id)

    def set_screening_override(
        self, issuer_id: int, *, eligible: bool
    ) -> dict[str, Any]:
        issuer = self._issuer(issuer_id)
        now = utc_now()
        reason = (
            "Manuellt inkluderad från adminmenyn"
            if eligible
            else "Manuellt exkluderad från adminmenyn"
        )
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO issuer_screening_overrides (
                    issuer_id, eligible, reason, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(issuer_id) DO UPDATE SET
                    eligible=excluded.eligible,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (issuer_id, int(eligible), reason, now),
            )
            active = connection.execute(
                """
                SELECT id FROM screening_batches
                WHERE status IN ('queued', 'running')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            batch_id = int(active["id"]) if active else None
            if batch_id is not None and eligible:
                connection.execute(
                    """
                    UPDATE screening_batch_items
                    SET status='pending', priority=0, decision=NULL,
                        reason=?, completed_at=NULL, updated_at=?
                    WHERE batch_id=? AND issuer_id=? AND status='skipped'
                    """,
                    (reason, now, batch_id, issuer_id),
                )
            elif batch_id is not None and not eligible:
                connection.execute(
                    """
                    UPDATE screening_batch_items
                    SET status='skipped', decision='not_relevant',
                        reason=?, completed_at=?, updated_at=?
                    WHERE batch_id=? AND issuer_id=? AND status='pending'
                    """,
                    (reason, now, now, batch_id, issuer_id),
                )
        next_action_id = (
            self._activate_next_batch_item(batch_id)
            if batch_id is not None
            else None
        )
        return {
            "issuer_id": issuer_id,
            "issuer": issuer["canonical_name"],
            "eligible": eligible,
            "reason": reason,
            "batch_id": batch_id,
            "_next_action_id": next_action_id,
        }

    def record_screening_action(self, action_id: int) -> int | None:
        now = utc_now()
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT bi.id AS item_id, bi.batch_id, a.status,
                       a.result_json, a.error
                FROM screening_batch_items bi
                JOIN manual_action_runs a ON a.id=bi.action_id
                WHERE bi.action_id=?
                """,
                (action_id,),
            ).fetchone()
        if not row or str(row["status"]) in {"queued", "running"}:
            return None
        result = _load_json_dict(row["result_json"])
        status = str(row["status"])
        decision = "unknown"
        need_score = None
        need_timing = None
        runway_months = None
        reason = str(row["error"] or "")
        item_status = status
        if status == "completed":
            assessment = result.get("assessment") or {}
            need_score = assessment.get("adjusted_need_score")
            need_timing = assessment.get("need_timing")
            runway_months = assessment.get("adjusted_runway_months")
            if not isinstance(need_score, (int, float)) or not need_timing:
                decision = "unknown"
                reason = (
                    "Körningen saknar ett fullständigt validerat "
                    "kapitalbehovsbeslut"
                )
            else:
                has_need = (
                    need_score > 0
                    and need_timing not in {
                        "no_negative_operating_cash_flow_signal",
                        "more_than_24_months",
                    }
                )
                decision = "yes" if has_need else "no"
                reason = (
                    f"Kapitalbehov identifierat: {need_timing}"
                    if has_need
                    else (
                        "Inget kapitalbehov enligt validerad operativ "
                        "kassaflödesprofil"
                    )
                )
        elif status == "insufficient_facts":
            reason = str(
                result.get("reason")
                or "Otillräckliga rapportfakta för beslut"
            )
            if result.get("screening_outcome") == "api_limit_reached":
                item_status = "deferred"
        elif status == "cancelled":
            reason = reason or "Batchscreeningen stoppades manuellt"
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE screening_batch_items
                SET status=?, decision=?, need_score=?, need_timing=?,
                    runway_months=?, reason=?, completed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    item_status,
                    decision,
                    need_score,
                    need_timing,
                    runway_months,
                    reason[:4000],
                    now,
                    now,
                    row["item_id"],
                ),
            )
        return self._activate_next_batch_item(int(row["batch_id"]))

    def resume_screening_batches(self) -> list[int]:
        with self.db.connect() as connection:
            terminal_actions = [
                int(row["action_id"])
                for row in connection.execute(
                    """
                    SELECT bi.action_id
                    FROM screening_batch_items bi
                    JOIN screening_batches b ON b.id=bi.batch_id
                    JOIN manual_action_runs a ON a.id=bi.action_id
                    WHERE b.status IN ('queued', 'running', 'stopping')
                      AND bi.status IN ('queued', 'running')
                      AND a.status NOT IN ('queued', 'running')
                    """
                ).fetchall()
            ]
            batch_ids = [
                int(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM screening_batches
                    WHERE status IN ('queued', 'running', 'stopping')
                    ORDER BY id
                    """
                ).fetchall()
            ]
        action_ids: list[int] = []
        for action_id in terminal_actions:
            next_id = self.record_screening_action(action_id)
            if next_id is not None:
                action_ids.append(next_id)
        for batch_id in batch_ids:
            next_id = self._activate_next_batch_item(batch_id)
            if next_id is not None:
                action_ids.append(next_id)
        return list(dict.fromkeys(action_ids))

    def _activate_next_batch_item(self, batch_id: int | None) -> int | None:
        if batch_id is None:
            return None
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                """
                SELECT status, stop_requested FROM screening_batches
                WHERE id=?
                """,
                (batch_id,),
            ).fetchone()
            if not batch or str(batch["status"]) not in {
                "queued",
                "running",
                "stopping",
            }:
                return None
            if batch["stop_requested"]:
                active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM screening_batch_items
                        WHERE batch_id=? AND status IN ('queued', 'running')
                        """,
                        (batch_id,),
                    ).fetchone()[0]
                )
                if active == 0:
                    connection.execute(
                        """
                        UPDATE screening_batches
                        SET status='stopped', completed_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, now, batch_id),
                    )
                return None
            existing = connection.execute(
                """
                SELECT bi.action_id, a.status
                FROM screening_batch_items bi
                JOIN manual_action_runs a ON a.id=bi.action_id
                WHERE bi.batch_id=? AND bi.status IN ('queued', 'running')
                ORDER BY bi.id LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            if existing:
                return (
                    int(existing["action_id"])
                    if existing["status"] == "queued"
                    else None
                )
            item = connection.execute(
                """
                SELECT id, issuer_id FROM screening_batch_items
                WHERE batch_id=? AND status='pending'
                ORDER BY priority, id LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            if not item:
                connection.execute(
                    """
                    UPDATE screening_batches
                    SET status='completed', completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, batch_id),
                )
                return None
            active_action = connection.execute(
                """
                SELECT id, status FROM manual_action_runs
                WHERE issuer_id=? AND action_type='interpret_capital_need'
                  AND status IN ('queued', 'running')
                ORDER BY id DESC LIMIT 1
                """,
                (item["issuer_id"],),
            ).fetchone()
            if active_action:
                action_id = int(active_action["id"])
                item_status = str(active_action["status"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO manual_action_runs (
                        issuer_id, action_type, status, phase, deadline_at,
                        started_at, updated_at
                    ) VALUES (
                        ?, 'interpret_capital_need', 'queued', 'queued',
                        NULL, ?, ?
                    )
                    """,
                    (item["issuer_id"], now, now),
                )
                action_id = int(cursor.lastrowid)
                item_status = "queued"
                self._append_progress_event(
                    connection,
                    action_id=action_id,
                    phase="queued",
                    status="queued",
                    metadata={
                        "trigger_kind": "manual_batch",
                        "batch_id": batch_id,
                    },
                    created_at=now,
                )
            connection.execute(
                """
                UPDATE screening_batch_items
                SET action_id=?, status=?, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (action_id, item_status, now, item["id"]),
            )
            connection.execute(
                """
                UPDATE screening_batches
                SET status='running', started_at=COALESCE(started_at, ?),
                    updated_at=?
                WHERE id=?
                """,
                (now, now, batch_id),
            )
            return action_id if item_status == "queued" else None

    def claim_action(self, action_id: int) -> tuple[str, str] | None:
        self.recover_expired_actions()
        lease_token = uuid4().hex
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT deadline_at FROM manual_action_runs
                WHERE id=? AND status='queued'
                """,
                (action_id,),
            ).fetchone()
            if not row:
                return None
            deadline_at = (
                str(row["deadline_at"])
                if row["deadline_at"]
                else (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.settings.action_timeout)
                ).replace(microsecond=0).isoformat()
            )
            if deadline_at <= now:
                cursor = connection.execute(
                    """
                    UPDATE manual_action_runs
                    SET status='timed_out', phase='timed_out',
                        completed_at=?, updated_at=?,
                        error='Åtgärden överskred sin absoluta deadline'
                    WHERE id=? AND status='queued'
                    """,
                    (now, now, action_id),
                )
                if cursor.rowcount == 1:
                    self._record_analysis_attempt(
                        connection,
                        action_id=action_id,
                        status="timed_out",
                        failure_reason=(
                            "Åtgärden överskred sin absoluta deadline"
                        ),
                        attempted_at=now,
                    )
                    self._append_progress_event(
                        connection,
                        action_id=action_id,
                        phase="timed_out",
                        status="timed_out",
                        message="Åtgärden överskred sin absoluta deadline",
                        metadata={"deadline_at": deadline_at},
                        created_at=now,
                    )
                return None
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='running', phase='selecting_report', lease_token=?,
                    deadline_at=?, updated_at=?, error=NULL
                WHERE id=? AND status='queued'
                """,
                (lease_token, deadline_at, now, action_id),
            )
            if cursor.rowcount != 1:
                return None
            # Registrera forsoket direkt vid paborjad analys sa att
            # skyddsregeln (min-interval mellan forsok) i
            # select_analysis_candidates biter aven om analysen kraschar.
            # Ror inte analysis_status har — den satts forst nar analysen
            # faktiskt avslutats (completed/failed/timed_out).
            connection.execute(
                """
                INSERT INTO issuer_analysis_summary (
                    issuer_id, last_action_id, last_attempt_at, updated_at
                )
                SELECT issuer_id, id, ?, ?
                FROM manual_action_runs
                WHERE id=? AND action_type='interpret_capital_need'
                ON CONFLICT(issuer_id) DO UPDATE SET
                    last_action_id=excluded.last_action_id,
                    last_attempt_at=excluded.last_attempt_at,
                    updated_at=excluded.updated_at
                """,
                (now, now, action_id),
            )
            self._append_progress_event(
                connection,
                action_id=action_id,
                phase="selecting_report",
                status="running",
                metadata={"deadline_at": deadline_at},
                created_at=now,
            )
            connection.execute(
                """
                UPDATE screening_batch_items
                SET status='running', updated_at=?
                WHERE action_id=? AND status='queued'
                """,
                (now, action_id),
            )
            return lease_token, deadline_at

    def run_claimed_action(
        self, action_id: int, lease_token: str, deadline_at: str
    ) -> None:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT a.issuer_id, a.action_type, b.id AS batch_id,
                       COALESCE(b.refresh_mfn, 0) AS refresh_mfn
                FROM manual_action_runs a
                LEFT JOIN screening_batch_items bi ON bi.action_id=a.id
                LEFT JOIN screening_batches b ON b.id=bi.batch_id
                WHERE a.id=? AND a.status='running' AND a.lease_token=?
                """,
                (action_id, lease_token),
            ).fetchone()
        if not row:
            return
        issuer_id = int(row["issuer_id"])
        # MFN-hämtning är standard i tolkningsflödet (hämta nya dokument
        # innan analys). En screening-batch kan uttryckligen stänga av den
        # via refresh_mfn=0; för fristående/auto-körningar (batch_id NULL)
        # är den alltid på.
        is_batch = row["batch_id"] is not None
        refresh_mfn_first = bool(row["refresh_mfn"]) if is_batch else True
        try:
            with self.db.connect() as connection:
                previous = connection.execute(
                    """SELECT adjusted_runway_months FROM runway_assessments
                       WHERE issuer_id=? ORDER BY id DESC LIMIT 1""",
                    (issuer_id,),
                ).fetchone()
            before_runway = previous["adjusted_runway_months"] if previous else None
            output, terminal_status = self._interpret_core(
                issuer_id,
                deadline_at=deadline_at,
                refresh_mfn_first=refresh_mfn_first,
                screening_batch_id=(
                    int(row["batch_id"])
                    if row["batch_id"] is not None
                    else None
                ),
                force_reinterpret=str(row["action_type"]).startswith("force_reinterpret"),
                force_key=f"force-action:{action_id}",
                phase=lambda value, metadata=None: self._update_action_phase(
                    action_id, lease_token, value, metadata=metadata
                ),
            )
            assessment = output.get("assessment")
            after_runway = (
                assessment.get("adjusted_runway_months")
                if isinstance(assessment, dict) else None
            )
            output["runway_comparison"] = _runway_comparison(
                before_runway, after_runway
            )
            saving_metadata: dict[str, Any] = {
                "runway_status": output.get("runway_status"),
            }
            if isinstance(assessment, dict):
                saving_metadata["document_id"] = assessment.get(
                    "report_document_id"
                )
                saving_metadata["bergs_fit"] = assessment.get("bergs_fit")
            self._update_action_phase(
                action_id,
                lease_token,
                "saving",
                metadata=saving_metadata,
            )
            self._complete_claimed_action(
                action_id,
                lease_token,
                terminal_status,
                output,
            )
        except ActionTimedOut as exc:
            self._fail_claimed_action(
                action_id,
                lease_token,
                "timed_out",
                str(exc),
            )
        except Exception as exc:
            self._fail_claimed_action(
                action_id,
                lease_token,
                "failed",
                str(exc),
            )

    def _interpret_core(
        self,
        issuer_id: int,
        *,
        deadline_at: str,
        refresh_mfn_first: bool = True,
        screening_batch_id: int | None = None,
        force_reinterpret: bool = False,
        force_key: str | None = None,
        phase,
    ) -> tuple[dict[str, Any], str]:  # type: ignore[no-untyped-def]
        issuer = self._issuer(issuer_id)
        self._assert_before_deadline(deadline_at)
        refresh_result: dict[str, Any] | None = None
        refresh_error: str | None = None
        if refresh_mfn_first:
            phase("refreshing", {"issuer_id": issuer_id})
            try:
                refresh_result = self.refresh_mfn(issuer_id)
            except Exception as exc:
                refresh_error = str(exc)
                LOGGER.warning(
                    "mfn_refresh_failed issuer=%s error=%s",
                    issuer_id,
                    refresh_error,
                )
            self._assert_before_deadline(deadline_at)
        pending = self.store.fetch_candidates(
            1, issuer_id=issuer_id, prefer_reports=True
        )
        candidate_metadata: dict[str, Any] = {}
        if pending:
            candidate_metadata = {
                "document_id": pending[0].document_id,
                "document_title": pending[0].title,
            }
        phase("selecting_report", candidate_metadata)
        prepared = {
            "seen": 0,
            "prepared": 0,
            "needs_ocr": 0,
            "failed": 0,
            "documents": [],
        }
        if pending:
            phase("preparing_text", candidate_metadata)
            prepared = prepare_documents(
                self.store,
                self.settings,
                limit=1,
                issuer_id=issuer_id,
                prefer_reports=True,
            )
        readiness = self.store.latest_report_readiness(issuer_id)
        selected_metadata = dict(candidate_metadata)
        if readiness:
            selected_metadata.update(
                {
                    "document_id": readiness.get("id"),
                    "document_title": readiness.get("title"),
                    "source_url": readiness.get("source_url"),
                    "page_count": readiness.get("page_count"),
                    "character_count": readiness.get("char_count"),
                }
            )
        phase("preparing_text", selected_metadata)
        if not readiness or readiness["readiness"] != "complete":
            reason = (
                readiness["reason"]
                if readiness
                else "Ingen kvartalsrapport hittades"
            )
            if refresh_error:
                reason = f"{reason}. MFN-uppdateringen misslyckades: {refresh_error}"
            fallback = self._try_web_fallback(
                issuer_id, selected_metadata, phase, deadline_at=deadline_at
            )
            if fallback is not None:
                return fallback
            return (
                {
                    "issuer_id": issuer_id,
                    "issuer": issuer["canonical_name"],
                    "prepared": prepared,
                    "analysis": {
                        "seen": 0,
                        "completed": 0,
                        "skipped_idempotent": 0,
                        "failed": 0,
                        "documents": [],
                    },
                    "runway": None,
                    "runway_status": "insufficient_facts",
                    "reason": reason,
                    "mfn_refresh": refresh_result,
                    "mfn_refresh_error": refresh_error,
                    "watching": True,
                },
                "insufficient_facts",
            )
        self._assert_before_deadline(deadline_at)
        analysis = analyze_documents(
            self.store,
            self.settings,
            limit=1,
            dry_run=False,
            issuer_id=issuer_id,
            prefer_reports=True,
            deadline_at=deadline_at,
            before_provider_call=(
                (
                    lambda: self._reserve_screening_api_call(
                        screening_batch_id
                    )
                )
                if screening_batch_id is not None
                else None
            ),
            progress_callback=phase,
            force_reinterpret=force_reinterpret,
            force_key=force_key,
        )
        if analysis.get("api_limit_reached"):
            reason = (
                "Batchens hårda tak för nya Kimi-anrop är nått. "
                "Bolaget har skjutits upp utan API-anrop."
            )
            return (
                {
                    "issuer_id": issuer_id,
                    "issuer": issuer["canonical_name"],
                    "prepared": prepared,
                    "analysis": analysis,
                    "runway": None,
                    "runway_status": "deferred",
                    "screening_outcome": "api_limit_reached",
                    "reason": reason,
                    "mfn_refresh": refresh_result,
                    "mfn_refresh_error": refresh_error,
                    "watching": True,
                },
                "insufficient_facts",
            )
        if analysis.get("provider_error"):
            if any(
                item.get("status") == "timed_out"
                for item in analysis.get("documents", [])
            ):
                raise ActionTimedOut(str(analysis["provider_error"]))
            raise RuntimeError(str(analysis["provider_error"]))
        if analysis.get("already_running"):
            raise RuntimeError(
                "En annan AI-tolkning använder redan samma rapport."
            )
        if (
            analysis["seen"] == 0
            and analysis["skipped_idempotent"] == 0
        ):
            # Readiness är redan verifierad som "complete" ovan (annars hade
            # vi returnerat tidigt). seen==0 här betyder därför att rapportens
            # råtext rensats efter en tidigare, lyckad extraction — inte att
            # rapporten saknas. Då ska vi INTE avbryta: post-rapport-passet
            # nedan ska fortfarande köras så att PM om emissioner/lån efter
            # rapportdatum extraheras och räknas in i kassan. Det är hela
            # syftet med att köra om ett redan-analyserat bolag.
            LOGGER.info(
                "interpret_report_already_extracted issuer=%s "
                "document=%s — fortsätter till post-rapport-pass",
                issuer_id,
                selected_metadata.get("document_id"),
            )
        self._assert_before_deadline(deadline_at)
        # Post-rapport-pass: extrahera köade PM publicerade EFTER rapportperiodens
        # slut (t.ex. nyemission/lån) så att runway kan justera kassan för
        # finansiering som skett efter rapportdatum. Utan detta flaggas bolag
        # som redan löst sin finansiering felaktigt för akut kapitalbehov.
        post_report_analysis: dict[str, Any] | None = None
        report_period_end = self.store.latest_report_period_end(issuer_id)
        if report_period_end:
            # Förbered texten för köade PM efter rapportperioden FÖRE urvalet.
            # ``post_report_candidates`` kräver ``document_contents.status=
            # 'extracted'``, men refresh_mfn skapar bara metadata-rader — utan
            # detta steg extraheras aldrig PM om genomförda emissioner, och
            # bolag som redan löst sin finansiering flaggas felaktigt.
            try:
                prepare_documents(
                    self.store,
                    self.settings,
                    limit=25,
                    issuer_id=issuer_id,
                    prefer_reports=False,
                )
            except Exception as exc:  # noqa: BLE001
                # Ett förberedelsefel får inte stoppa huvudanalysen.
                LOGGER.warning(
                    "post_report_prepare_failed issuer=%s error=%s",
                    issuer_id,
                    exc,
                )
            self._assert_before_deadline(deadline_at)
            post_report_candidates = self.store.post_report_candidates(
                issuer_id,
                report_period_end,
                exclude_document_id=selected_metadata.get("document_id"),
            )
            if post_report_candidates:
                phase(
                    "analyzing_post_report",
                    {
                        "issuer_id": issuer_id,
                        "report_period_end": report_period_end,
                        "post_report_documents": len(post_report_candidates),
                    },
                )
                try:
                    post_report_analysis = analyze_documents(
                        self.store,
                        self.settings,
                        limit=len(post_report_candidates),
                        dry_run=False,
                        issuer_id=issuer_id,
                        candidates=post_report_candidates,
                        deadline_at=deadline_at,
                        progress_callback=phase,
                        force_reinterpret=force_reinterpret,
                        force_key=force_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Ett fel i post-rapport-passet får inte stoppa huvudanalysen.
                    LOGGER.warning(
                        "post_report_analysis_failed issuer=%s error=%s",
                        issuer_id,
                        exc,
                    )
                    post_report_analysis = None
                self._assert_before_deadline(deadline_at)
        phase("computing_runway", selected_metadata)
        runway = compute_runways(self.store)
        assessment = next(
            (
                item
                for item in runway["assessments"]
                if item["issuer"] == issuer["canonical_name"]
            ),
            None,
        )
        if assessment is None:
            reason = next(
                (
                    item["reason"]
                    for item in runway["skipped"]
                    if item["issuer"] == issuer["canonical_name"]
                ),
                "Rapporten saknar validerad kassa eller operativt kassaflöde",
            )
            fallback = self._try_web_fallback(
                issuer_id, selected_metadata, phase, deadline_at=deadline_at
            )
            if fallback is not None:
                return fallback
            return (
                {
                    "issuer_id": issuer_id,
                    "issuer": issuer["canonical_name"],
                    "prepared": prepared,
                    "analysis": analysis,
                    "runway": runway,
                    "runway_status": "insufficient_facts",
                    "reason": reason,
                    "mfn_refresh": refresh_result,
                    "mfn_refresh_error": refresh_error,
                    "watching": True,
                },
                "insufficient_facts",
            )
        self.store.set_watchlist(
            issuer_id, active=True, interpreted=True
        )
        purge_result: dict[str, int] = {}
        try:
            purge_result = self.store.purge_document_raw_content(issuer_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "document_purge_failed issuer=%s error=%s", issuer_id, exc
            )
        return (
            {
                "issuer_id": issuer_id,
                "issuer": issuer["canonical_name"],
                "prepared": prepared,
                "analysis": analysis,
                "post_report_analysis": post_report_analysis,
                "runway": runway,
                "runway_status": "completed",
                "document_purge": purge_result,
                "assessment": assessment,
                "mfn_refresh": refresh_result,
                "mfn_refresh_error": refresh_error,
                "watching": True,
            },
            "completed",
        )

    def _try_web_fallback(
        self,
        issuer_id: int,
        selected_metadata: dict[str, Any],
        phase: Any,
        *,
        deadline_at: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """Webb-fallback vid insufficient_facts.

        Webbsökningen (web_fallback/research) är inte porterad till Capital
        Radar; huvudflödet fortsätter som insufficient_facts.
        """
        return None

    def recover_expired_actions(self) -> dict[str, int]:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                """
                SELECT id, deadline_at FROM manual_action_runs
                WHERE status IN ('queued', 'running')
                  AND deadline_at IS NOT NULL AND deadline_at<?
                ORDER BY id
                """,
                (now,),
            ).fetchall()
            actions = 0
            for row in expired:
                cursor = connection.execute(
                    """
                    UPDATE manual_action_runs
                    SET status='timed_out', phase='timed_out',
                        lease_token=NULL, completed_at=?, updated_at=?,
                        error='Åtgärden överskred sin absoluta deadline'
                    WHERE id=? AND status IN ('queued', 'running')
                    """,
                    (now, now, row["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                actions += 1
                self._record_analysis_attempt(
                    connection,
                    action_id=int(row["id"]),
                    status="timed_out",
                    failure_reason=(
                        "Åtgärden överskred sin absoluta deadline"
                    ),
                    attempted_at=now,
                )
                self._append_progress_event(
                    connection,
                    action_id=int(row["id"]),
                    phase="timed_out",
                    status="timed_out",
                    message="Åtgärden överskred sin absoluta deadline",
                    metadata={"deadline_at": row["deadline_at"]},
                    created_at=now,
                )
            # Progress-watchdog: fanga actions som hanger i 'running' utan
            # nagon uppdatering pa >5 min (t.ex. worker som fastnat i
            # computing_runway vid serverinstabilitet), men som annu inte
            # natt sin absoluta deadline. Deadline-loopen ovan fangar dem
            # forst vid deadline (~12 min) — denna regel fangar dem tidigare.
            # Cutoff beraknas i Python (samma ISO-format som updated_at).
            stalled_cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).replace(microsecond=0).isoformat()
            stalled = connection.execute(
                """
                SELECT id FROM manual_action_runs
                WHERE status='running'
                  AND updated_at IS NOT NULL
                  AND updated_at < ?
                  AND (deadline_at IS NULL OR deadline_at >= ?)
                ORDER BY id
                """,
                (stalled_cutoff, now),
            ).fetchall()
            for row in stalled:
                cursor = connection.execute(
                    """
                    UPDATE manual_action_runs
                    SET status='timed_out', phase='timed_out',
                        lease_token=NULL, completed_at=?, updated_at=?,
                        error='Ingen progress inom 5 min (worker hängde?)'
                    WHERE id=? AND status='running'
                    """,
                    (now, now, row["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                actions += 1
                self._record_analysis_attempt(
                    connection,
                    action_id=int(row["id"]),
                    status="timed_out",
                    failure_reason="Ingen progress inom 5 min (worker hängde?)",
                    attempted_at=now,
                )
                self._append_progress_event(
                    connection,
                    action_id=int(row["id"]),
                    phase="timed_out",
                    status="timed_out",
                    message="Ingen progress inom 5 min (worker hängde?)",
                    created_at=now,
                )
        jobs = self.store.recover_stale_jobs()
        return {"actions": int(actions), "jobs": jobs}

    def _update_action_phase(
        self,
        action_id: int,
        lease_token: str,
        phase: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET phase=?, updated_at=?
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (phase, now, action_id, lease_token),
            )
            updated = cursor.rowcount == 1
            if updated:
                self._append_progress_event(
                    connection,
                    action_id=action_id,
                    phase=phase,
                    status="running",
                    metadata=metadata,
                    created_at=now,
                )
        if updated:
            LOGGER.info("ai_phase action=%s phase=%s", action_id, phase)
        return updated

    def _complete_claimed_action(
        self,
        action_id: int,
        lease_token: str,
        status: str,
        result: dict[str, Any],
    ) -> bool:
        if status not in {"completed", "insufficient_facts"}:
            raise ValueError(status)
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status=?, phase=?, lease_token=NULL, completed_at=?,
                    updated_at=?, result_json=?, error=NULL
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    status,
                    status,
                    now,
                    now,
                    json.dumps(result, ensure_ascii=False),
                    action_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if status == "insufficient_facts":
                self._record_analysis_attempt(
                    connection,
                    action_id=action_id,
                    status=status,
                    failure_reason=str(
                        result.get("reason")
                        or "Otillräckliga fakta för kapitalbehovsbedömning"
                    ),
                    attempted_at=now,
                )
            metadata: dict[str, Any] = {}
            assessment = result.get("assessment")
            if isinstance(assessment, dict):
                metadata = {
                    "document_id": assessment.get("report_document_id"),
                    "bergs_fit": assessment.get("bergs_fit"),
                }
            metadata["runway_comparison"] = result.get("runway_comparison")
            if status == "insufficient_facts":
                metadata["outcome"] = "awaiting_sources"
            self._append_progress_event(
                connection,
                action_id=action_id,
                phase=status,
                status=status,
                metadata=metadata,
                created_at=now,
            )
            return True

    def _fail_claimed_action(
        self,
        action_id: int,
        lease_token: str,
        status: str,
        error: str,
    ) -> bool:
        if status not in {"failed", "timed_out"}:
            raise ValueError(status)
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status=?, phase=?, lease_token=NULL, completed_at=?,
                    updated_at=?, error=?
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    status,
                    status,
                    now,
                    now,
                    error[:4000],
                    action_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._record_analysis_attempt(
                connection,
                action_id=action_id,
                status=status,
                failure_reason=error,
                attempted_at=now,
            )
            self._append_progress_event(
                connection,
                action_id=action_id,
                phase=status,
                status=status,
                message=error[:1000],
                created_at=now,
            )
            return True

    @staticmethod
    def _record_analysis_attempt(
        connection: sqlite3.Connection,
        *,
        action_id: int,
        status: str,
        failure_reason: str,
        attempted_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO issuer_analysis_summary (
                issuer_id, last_action_id, analysis_status,
                last_attempt_at, failure_reason, updated_at
            )
            SELECT issuer_id, id, ?, ?, ?, ?
            FROM manual_action_runs
            WHERE id=? AND action_type='interpret_capital_need'
            ON CONFLICT(issuer_id) DO UPDATE SET
                last_action_id=excluded.last_action_id,
                last_attempt_at=excluded.last_attempt_at,
                failure_reason=excluded.failure_reason,
                analysis_status=CASE
                    WHEN issuer_analysis_summary.current_analysis_version_id
                         IS NULL
                    THEN excluded.analysis_status
                    ELSE issuer_analysis_summary.analysis_status
                END,
                updated_at=excluded.updated_at
            """,
            (
                status,
                attempted_at,
                failure_reason[:4000],
                attempted_at,
                action_id,
            ),
        )

    @staticmethod
    def _append_progress_event(
        connection: sqlite3.Connection,
        *,
        action_id: int,
        phase: str,
        status: str,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> int:
        event_metadata = dict(metadata or {})
        document_id = _optional_int(event_metadata.get("document_id"))
        extraction_job_id = _optional_int(
            event_metadata.get("extraction_job_id")
        )
        estimated_tokens = _optional_int(
            event_metadata.get("estimated_input_tokens")
        )
        usage = event_metadata.get("usage")
        actual_tokens = None
        if isinstance(usage, dict):
            actual_tokens = _optional_int(usage.get("total_tokens"))
        if actual_tokens is None:
            actual_tokens = _optional_int(event_metadata.get("actual_tokens"))
        sequence_no = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(sequence_no), 0) + 1
                FROM action_progress_events
                WHERE action_id=?
                """,
                (action_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """
            INSERT INTO action_progress_events (
                action_id, sequence_no, phase, status, message,
                document_id, extraction_job_id, estimated_tokens,
                actual_tokens, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                sequence_no,
                phase,
                status,
                message or PROGRESS_MESSAGES.get(phase),
                document_id,
                extraction_job_id,
                estimated_tokens,
                actual_tokens,
                json.dumps(event_metadata, ensure_ascii=False),
                created_at or utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _assert_before_deadline(deadline_at: str) -> None:
        if datetime.now(timezone.utc) >= datetime.fromisoformat(deadline_at):
            raise ActionTimedOut(
                "AI-tolkningen överskred action-timeouten"
            )

    def interpret(self, issuer_id: int) -> dict[str, Any]:
        action = self.enqueue_interpret(issuer_id)
        claimed = self.claim_action(int(action["id"]))
        if not claimed:
            return self.action_status(int(action["id"]))
        self.run_claimed_action(int(action["id"]), *claimed)
        status = self.action_status(int(action["id"]))
        if status["status"] in {"failed", "timed_out"}:
            raise RuntimeError(str(status["error"]))
        return dict(status["result"] or {})

    def _screening_universe(
        self,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, tuple[bool, str | None]],
    ]:
        with self.db.connect() as connection:
            security_rows = connection.execute(
                """
                SELECT i.id AS issuer_id, i.canonical_name,
                       i.jurisdiction_code, s.metadata_json
                FROM issuers i
                JOIN securities s ON s.issuer_id=i.id
                WHERE s.active=1
                ORDER BY i.canonical_name, s.id
                """
            ).fetchall()
            override_rows = connection.execute(
                """
                SELECT issuer_id, eligible, reason
                FROM issuer_screening_overrides
                """
            ).fetchall()
        overrides = {
            int(row["issuer_id"]): (
                bool(row["eligible"]),
                row["reason"],
            )
            for row in override_rows
        }
        issuers: dict[int, dict[str, Any]] = {}
        for row in security_rows:
            issuer_id = int(row["issuer_id"])
            issuer = issuers.setdefault(
                issuer_id,
                {
                    "name": str(row["canonical_name"]),
                    "jurisdiction_code": row["jurisdiction_code"],
                    "sectors": set(),
                },
            )
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            sector = str(metadata.get("sector") or "").strip()
            if sector:
                issuer["sectors"].add(sector)
        return issuers, overrides

    def _latest_report_states(
        self, issuer_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        states: dict[int, dict[str, Any]] = {}
        with self.db.connect() as connection:
            for issuer_id in issuer_ids:
                report = connection.execute(
                    """
                    SELECT id FROM documents
                    WHERE issuer_id=? AND profile_category='report'
                      AND queued_for_analysis=1
                    ORDER BY published_at DESC, id ASC
                    LIMIT 1
                    """,
                    (issuer_id,),
                ).fetchone()
                if not report:
                    states[issuer_id] = {
                        "complete": False,
                        "cached": False,
                        "code": "no_report",
                        "reason": "Ingen lokal kvartalsrapport hittades",
                    }
                    continue
                document_id = int(report["id"])
                content = connection.execute(
                    """
                    SELECT status, quality_status, quality_reason
                    FROM document_contents
                    WHERE document_id=?
                    ORDER BY
                        CASE
                            WHEN quality_status='complete' THEN 0
                            WHEN quality_status='not_required' THEN 1
                            WHEN status='extracted' THEN 2
                            WHEN status='needs_ocr' THEN 3
                            ELSE 4
                        END,
                        quality_score DESC, char_count DESC, id
                    LIMIT 1
                    """,
                    (document_id,),
                ).fetchone()
                complete = bool(
                    content and content["quality_status"] == "complete"
                )
                cached = bool(
                    complete
                    and connection.execute(
                        """
                        SELECT 1 FROM extraction_jobs
                        WHERE document_id=? AND status='completed'
                        LIMIT 1
                        """,
                        (document_id,),
                    ).fetchone()
                )
                if complete:
                    code = "cached" if cached else "ready_for_kimi"
                    reason = (
                        "Rapporten är redan Kimi-extraherad"
                        if cached
                        else "Komplett rapport är redo för Kimi"
                    )
                elif content and content["status"] == "needs_ocr":
                    code = "needs_ocr"
                    reason = "Senaste rapporten behöver OCR"
                elif content:
                    code = "incomplete_report"
                    reason = str(
                        content["quality_reason"]
                        or "Rapporten saknar fullständiga runway-fakta"
                    )
                else:
                    code = "unprepared_report"
                    reason = "Rapporten finns men har inte förberetts lokalt"
                states[issuer_id] = {
                    "document_id": document_id,
                    "complete": complete,
                    "cached": cached,
                    "code": code,
                    "reason": reason,
                }
        return states

    def _reserve_screening_api_call(self, batch_id: int) -> bool:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE screening_batches
                SET api_calls_used=api_calls_used+1, updated_at=?
                WHERE id=? AND status='running' AND stop_requested=0
                  AND api_calls_used < max_api_calls
                """,
                (utc_now(), batch_id),
            )
            return cursor.rowcount == 1

    def _issuer(self, issuer_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT id, canonical_name
                FROM issuers
                WHERE id=? AND EXISTS (
                    SELECT 1 FROM securities s
                    WHERE s.issuer_id=issuers.id AND s.active=1
                )
                """,
                (issuer_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"Bolaget finns inte i det aktiva universumet: {issuer_id}")
        return dict(row)

    def _resolve_mfn_identity(
        self, issuer_id: int, issuer_name: str
    ) -> tuple[str, str]:
        with self.db.connect() as connection:
            known_slug = known_mfn_slug(connection, issuer_id)
            if known_slug:
                return known_slug, f"{BASE_URL}/all/a/{known_slug}"
            rows = connection.execute(
                """
                SELECT ds.metadata_json
                FROM document_sources ds
                JOIN documents d ON d.id=ds.document_id
                WHERE ds.source_id='mfn' AND d.issuer_id=?
                ORDER BY ds.last_seen_at DESC
                """,
                (issuer_id,),
            ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            slug = str(metadata.get("issuer_slug") or "")
            url = str(metadata.get("issuer_url") or "")
            if _VALID_SLUG.fullmatch(slug) and url.rstrip("/") == (
                f"{BASE_URL}/all/a/{slug}"
            ):
                return slug, url.rstrip("/")
        slug = _slugify(issuer_name)
        if not slug:
            raise RuntimeError(
                "Kunde inte härleda MFN-sluggen. Bolaget behöver först "
                "upptäckas i MFN:s huvudflöde."
            )
        return slug, f"{BASE_URL}/all/a/{slug}"

    def _start_action(self, issuer_id: int, action_type: str) -> int:
        self.recover_expired_actions()
        now = utc_now()
        deadline = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.settings.action_timeout)
        ).replace(microsecond=0).isoformat()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO manual_action_runs (
                    issuer_id, action_type, status, phase, deadline_at,
                    started_at, updated_at
                ) VALUES (?, ?, 'running', 'refreshing', ?, ?, ?)
                """,
                (issuer_id, action_type, deadline, now, now),
            )
            action_id = int(cursor.lastrowid)
            self._append_progress_event(
                connection,
                action_id=action_id,
                phase="refreshing",
                status="running",
                metadata={
                    "trigger_kind": "manual",
                    "deadline_at": deadline,
                },
                created_at=now,
            )
            return action_id

    def _finish_action(self, action_id: int, result: dict[str, Any]) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='completed', phase='completed', completed_at=?,
                    updated_at=?, result_json=?, error=NULL
                WHERE id=?
                """,
                (
                    now,
                    now,
                    json.dumps(result, ensure_ascii=False),
                    action_id,
                ),
            )
            if cursor.rowcount == 1:
                self._append_progress_event(
                    connection,
                    action_id=action_id,
                    phase="completed",
                    status="completed",
                    created_at=now,
                )

    def _fail_action(self, action_id: int, error: str) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='failed', phase='failed', completed_at=?,
                    updated_at=?, error=?
                WHERE id=?
                """,
                (now, now, error[:4000], action_id),
            )
            if cursor.rowcount == 1:
                self._append_progress_event(
                    connection,
                    action_id=action_id,
                    phase="failed",
                    status="failed",
                    message=error[:1000],
                    created_at=now,
                )


_VALID_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-?")


def _slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"\b(?:ab|publ|plc|ltd|oyj|asa)\b", " ", text, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _load_json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



def _runway_comparison(before: Any, after: Any) -> dict[str, Any]:
    def numeric(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    before = before if numeric(before) else None
    after = after if numeric(after) else None
    comparable = numeric(before) and numeric(after)
    delta = after - before if comparable else None
    return {
        "before_months": before,
        "after_months": after,
        "delta_months": delta,
        "tolerance_months": 1,
        "within_tolerance": abs(delta) <= 1 if comparable else None,
        "outcome": (
            "awaiting_sources" if not numeric(after)
            else "initial_analysis" if not numeric(before)
            else "within_tolerance" if abs(delta) <= 1
            else "runway_changed"
        ),
    }
