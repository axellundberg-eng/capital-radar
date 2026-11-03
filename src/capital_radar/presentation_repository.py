from __future__ import annotations

import base64
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .normalization import utc_now


class PresentationRepository:
    """Read models and small user-owned writes for the dashboard."""

    def __init__(
        self,
        database_path: Path,
        *,
        mfn_parallel_channels: int = 4,
    ) -> None:
        self.database_path = database_path.resolve()
        self.mfn_parallel_channels = max(
            1, min(8, int(mfn_parallel_channels))
        )

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def overview(self) -> dict[str, Any]:
        candidates = self.list_issuers(
            {
                "limit": 10,
                "sort": "priority",
                "analysis_status": "completed",
            }
        )
        now = utc_now()
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_issuers,
                    SUM(CASE WHEN s.analysis_status='completed'
                             THEN 1 ELSE 0 END) AS analyzed,
                    SUM(CASE WHEN s.capital_need_decision='yes'
                             THEN 1 ELSE 0 END) AS capital_need,
                    SUM(CASE WHEN s.capital_need_decision='yes'
                                  AND COALESCE(s.adjusted_runway_months,
                                               s.runway_months)<6
                             THEN 1 ELSE 0 END) AS urgent_under_6m,
                    SUM(CASE WHEN w.follow_up_at IS NOT NULL
                                  AND w.follow_up_at<=?
                             THEN 1 ELSE 0 END) AS follow_ups_due
                FROM issuers i
                LEFT JOIN issuer_analysis_summary s ON s.issuer_id=i.id
                LEFT JOIN issuer_workflow w ON w.issuer_id=i.id
                WHERE EXISTS (
                    SELECT 1 FROM securities sec
                    WHERE sec.issuer_id=i.id AND sec.active=1
                )
                """,
                (now,),
            ).fetchone()
        return {
            "generated_at": now,
            "metrics": {
                key: int(row[key] or 0)
                for key in (
                    "total_issuers",
                    "analyzed",
                    "capital_need",
                    "urgent_under_6m",
                    "follow_ups_due",
                )
            },
            "candidates": candidates["items"],
            "operations": self.operations_summary(),
        }

    def list_markets(self) -> list[str]:
        """Return the active-universe markets without paging every issuer."""
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT market
                FROM securities
                WHERE active=1 AND market IS NOT NULL AND TRIM(market)<>''
                ORDER BY market COLLATE NOCASE
                """
            ).fetchall()
        return [str(row["market"]) for row in rows]

    def list_issuers(self, filters: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(filters.get("limit") or 50), 100))
        sort = str(filters.get("sort") or "priority")
        default_direction = (
            "asc" if sort in {"name", "market", "runway", "follow_up_at"} else "desc"
        )
        direction = str(
            filters.get("direction") or default_direction
        ).casefold()
        if direction not in {"asc", "desc"}:
            direction = default_direction
        order_direction = direction.upper()
        cursor_operator = ">" if direction == "asc" else "<"
        where = [
            "EXISTS (SELECT 1 FROM securities active_sec "
            "WHERE active_sec.issuer_id=i.id AND active_sec.active=1)"
        ]
        params: list[Any] = []
        query = str(filters.get("q") or "").strip()
        if query:
            where.append(
                "(i.canonical_name LIKE ? OR sr.symbols LIKE ?)"
            )
            params.extend((f"%{query}%", f"%{query}%"))
        for key, expression in (
            ("market", "sr.market=?"),
            ("segment", "sr.segment=?"),
            ("analysis_status", "COALESCE(a.analysis_status, 'not_analyzed')=?"),
            ("workflow_status", "COALESCE(w.status, 'new')=?"),
            ("capital_need", "COALESCE(a.capital_need_decision, 'unknown')=?"),
        ):
            value = filters.get(key)
            if value not in (None, ""):
                where.append(expression)
                params.append(value)
        sector = str(filters.get("sector") or "").strip()
        if sector:
            where.append(
                "EXISTS (SELECT 1 FROM securities sector_sec "
                "WHERE sector_sec.issuer_id=i.id AND sector_sec.active=1 "
                "AND LOWER(sector_sec.metadata_json) LIKE ?)"
            )
            params.append(f"%{sector.casefold()}%")
        if filters.get("runway_max") is not None:
            where.append(
                "COALESCE(a.adjusted_runway_months, a.runway_months)"
                " IS NOT NULL AND"
                " COALESCE(a.adjusted_runway_months, a.runway_months)<=?"
            )
            params.append(float(filters["runway_max"]))
        if filters.get("fit_min") is not None:
            where.append(
                "a.bergs_fit_score IS NOT NULL AND a.bergs_fit_score>=?"
            )
            params.append(int(filters["fit_min"]))
        if _truthy(filters.get("report_ready")):
            where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM documents report_document
                    JOIN document_contents report_content
                      ON report_content.document_id=report_document.id
                    WHERE report_document.issuer_id=i.id
                      AND report_document.profile_category='report'
                      AND report_content.status='extracted'
                      AND report_content.quality_status='complete'
                )
                """
            )
        if _truthy(filters.get("follow_up_due")):
            where.append(
                "w.follow_up_at IS NOT NULL AND w.follow_up_at<=?"
            )
            params.append(utc_now())

        base_where = list(where)
        base_params = list(params)
        cursor = self._decode_cursor(filters.get("cursor"))
        order_sql: str
        cursor_value_sql: str
        if sort == "name":
            order_sql = (
                f"i.canonical_name COLLATE NOCASE {order_direction}, i.id ASC"
            )
            cursor_value_sql = "i.canonical_name"
            if cursor:
                where.append(
                    f"(i.canonical_name COLLATE NOCASE"
                    f"{cursor_operator}? COLLATE NOCASE OR "
                    "(i.canonical_name=? AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))
        elif sort == "market":
            # A market-first view is a useful working mode for coverage.  The
            # id is deliberately the only tie-breaker: it is also the cursor
            # tie-breaker, which keeps paging stable within each market.
            order_sql = (
                f"sr.market COLLATE NOCASE {order_direction}, i.id ASC"
            )
            cursor_value_sql = "sr.market"
            if cursor:
                where.append(
                    f"(sr.market COLLATE NOCASE {cursor_operator} ? COLLATE NOCASE OR "
                    "(sr.market=? AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))
        elif sort == "runway":
            order_sql = (
                "COALESCE(a.adjusted_runway_months, a.runway_months,"
                " 1000000000) "
                f"{order_direction}, i.id ASC"
            )
            cursor_value_sql = (
                "COALESCE(a.adjusted_runway_months, a.runway_months,"
                " 1000000000)"
            )
            if cursor:
                where.append(
                    "(COALESCE(a.adjusted_runway_months, a.runway_months,"
                    " 1000000000)"
                    f"{cursor_operator}? OR "
                    "(COALESCE(a.adjusted_runway_months, a.runway_months,"
                    " 1000000000)=? AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))
        elif sort == "updated":
            order_sql = (
                f"COALESCE(a.updated_at, '') {order_direction}, i.id ASC"
            )
            cursor_value_sql = "COALESCE(a.updated_at, '')"
            if cursor:
                where.append(
                    f"(COALESCE(a.updated_at, ''){cursor_operator}? OR "
                    "(COALESCE(a.updated_at, '')=? AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))
        elif sort == "follow_up_at":
            order_sql = (
                f"COALESCE(w.follow_up_at, '') {order_direction}, i.id ASC"
            )
            cursor_value_sql = "COALESCE(w.follow_up_at, '')"
            if cursor:
                where.append(
                    f"(COALESCE(w.follow_up_at, ''){cursor_operator}? OR "
                    "(COALESCE(w.follow_up_at, '')=? AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))
        else:
            sort = "priority"
            order_sql = (
                "COALESCE(a.effective_need_score, a.need_score, -1) "
                f"{order_direction}, i.id ASC"
            )
            cursor_value_sql = (
                "COALESCE(a.effective_need_score, a.need_score, -1)"
            )
            if cursor:
                where.append(
                    "(COALESCE(a.effective_need_score, a.need_score, -1)"
                    f"{cursor_operator}? OR "
                    "(COALESCE(a.effective_need_score, a.need_score, -1)=?"
                    " AND i.id>?))"
                )
                params.extend((cursor["value"], cursor["value"], cursor["id"]))

        cte = """
            WITH security_rollup AS (
                SELECT issuer_id, MIN(market) AS market,
                       MIN(segment) AS segment,
                       GROUP_CONCAT(symbol, ', ') AS symbols
                FROM securities WHERE active=1 GROUP BY issuer_id
            )
        """
        joins = """
            FROM issuers i
            JOIN security_rollup sr ON sr.issuer_id=i.id
            LEFT JOIN issuer_analysis_summary a ON a.issuer_id=i.id
            LEFT JOIN issuer_workflow w ON w.issuer_id=i.id
            LEFT JOIN issuer_crm c ON c.issuer_id=i.id
        """
        select = f"""
            SELECT i.id, i.canonical_name AS name, sr.market, sr.segment,
                   sr.symbols,
                   COALESCE(a.analysis_status, 'not_analyzed')
                       AS analysis_status,
                   COALESCE(a.capital_need_decision, 'unknown')
                       AS capital_need_decision,
                   COALESCE(a.effective_need_score, a.need_score)
                       AS need_score,
                   a.need_timing,
                   COALESCE(a.adjusted_runway_months, a.runway_months)
                       AS runway_months,
                   COALESCE(a.effective_alert_level, a.alert_level)
                       AS alert_level,
                   a.bergs_fit_score,
                   COALESCE(a.interpretation_headline, a.headline)
                       AS headline,
                   a.confidence,
                   a.updated_at AS analysis_updated_at,
                   COALESCE(w.status, 'new') AS workflow_status,
                   w.owner, w.next_action, w.follow_up_at,
                   COALESCE(c.status, 'missing') AS crm_status,
                   {cursor_value_sql} AS cursor_value
            {joins}
            WHERE {" AND ".join(where)}
            ORDER BY {order_sql}
            LIMIT ?
        """
        with self._read() as connection:
            rows = connection.execute(
                cte + select, (*params, limit + 1)
            ).fetchall()
            total = int(
                connection.execute(
                    cte
                    + "SELECT COUNT(*) "
                    + joins
                    + " WHERE "
                    + " AND ".join(base_where),
                    base_params,
                ).fetchone()[0]
            )
        has_more = len(rows) > limit
        visible = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in visible:
            item = dict(row)
            item.pop("cursor_value", None)
            items.append(item)
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = self._encode_cursor(
                last["cursor_value"], int(last["id"])
            )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "total_filtered": total,
            "sort": sort,
            "direction": direction,
        }

    def operations_summary(self) -> dict[str, Any]:
        now = utc_now()
        with self._read() as connection:
            action_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM manual_action_runs GROUP BY status
                """
            ).fetchall()
            current = connection.execute(
                """
                SELECT a.id, a.issuer_id, i.canonical_name AS issuer,
                       a.status, a.phase, a.started_at, a.updated_at,
                       a.deadline_at
                FROM manual_action_runs a
                JOIN issuers i ON i.id=a.issuer_id
                WHERE a.status='running'
                ORDER BY a.id LIMIT 1
                """
            ).fetchone()
            job_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM extraction_jobs GROUP BY status
                """
            ).fetchall()
            token_row = connection.execute(
                """
                SELECT COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cached_tokens), 0) AS cached_tokens
                FROM extraction_jobs
                WHERE substr(started_at, 1, 10)=substr(?, 1, 10)
                """,
                (now,),
            ).fetchone()
            estimated = connection.execute(
                """
                SELECT COALESCE(SUM(estimated_input_tokens), 0)
                FROM extraction_jobs WHERE status='running'
                """
            ).fetchone()[0]
            screening = connection.execute(
                """
                SELECT * FROM screening_batches ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            refresh = connection.execute(
                """
                SELECT * FROM report_refresh_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            # MFN-nyhetsbevakningen är inte porterad.
            monitor = None
            refresh_counts = None
            refresh_current = []
            if refresh:
                refresh_counts = connection.execute(
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
                    FROM report_refresh_items
                    WHERE run_id=?
                    """,
                    (int(refresh["id"]),),
                ).fetchone()
                refresh_current = connection.execute(
                    """
                    SELECT ri.issuer_id, i.canonical_name AS issuer,
                           ri.status, ri.attempt_count, ri.updated_at
                    FROM report_refresh_items ri
                    JOIN issuers i ON i.id=ri.issuer_id
                    WHERE ri.run_id=? AND ri.status='running'
                    ORDER BY ri.id LIMIT ?
                    """,
                    (
                        int(refresh["id"]),
                        self.mfn_parallel_channels,
                    ),
                ).fetchall()
        refresh_payload = dict(refresh) if refresh else None
        if refresh_payload is not None and refresh_counts is not None:
            progress = {
                key: int(value or 0)
                for key, value in dict(refresh_counts).items()
            }
            progress["finished"] = (
                progress["total"]
                - progress["pending"]
                - progress["running"]
            )
            current_items = [dict(row) for row in refresh_current]
            refresh_payload["progress"] = progress
            refresh_payload["current"] = (
                current_items[0] if current_items else None
            )
            refresh_payload["current_items"] = current_items
            refresh_payload["parallel_channels"] = (
                self.mfn_parallel_channels
            )
        return {
            "generated_at": now,
            "actions": {
                **{str(row["status"]): int(row["count"]) for row in action_rows},
                "current": dict(current) if current else None,
            },
            "kimi": {
                "jobs_by_status": {
                    str(row["status"]): int(row["count"]) for row in job_rows
                },
                "total_tokens_today": int(token_row["total_tokens"] or 0),
                "cached_tokens_today": int(token_row["cached_tokens"] or 0),
                "estimated_tokens_active": int(estimated or 0),
            },
            "screening": {"latest": dict(screening) if screening else None},
            "report_refresh": {"latest": refresh_payload},
            "mfn_monitor": (
                {
                    **dict(monitor),
                    "enabled": bool(monitor["enabled"]),
                }
                if monitor else {
                    "enabled": False,
                    "status": "disabled",
                    "interval_seconds": None,
                }
            ),
        }

    def issuer_extensions(self, issuer_id: int) -> dict[str, Any]:
        with self._read() as connection:
            summary = connection.execute(
                "SELECT * FROM issuer_analysis_summary WHERE issuer_id=?",
                (issuer_id,),
            ).fetchone()
            versions = connection.execute(
                """
                SELECT *
                FROM analysis_versions
                WHERE issuer_id=? ORDER BY version_no DESC LIMIT 25
                """,
                (issuer_id,),
            ).fetchall()
            workflow = connection.execute(
                "SELECT * FROM issuer_workflow WHERE issuer_id=?",
                (issuer_id,),
            ).fetchone()
        version_payloads = []
        for row in versions:
            item = dict(row)
            item["source_document_ids"] = _load_json(
                item.pop("source_document_ids_json"), []
            )
            version_payloads.append(item)
        return {
            "analysis_summary": dict(summary) if summary else None,
            "analysis_versions": version_payloads,
            "workflow": (
                dict(workflow)
                if workflow
                else {"issuer_id": issuer_id, "status": "new"}
            ),
        }

    @staticmethod
    def _encode_cursor(value: Any, issuer_id: int) -> str:
        raw = json.dumps(
            {"value": value, "id": issuer_id},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: Any) -> dict[str, Any] | None:
        if not value:
            return None
        try:
            text = str(value)
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "id" not in payload:
                raise ValueError
            return {"value": payload.get("value"), "id": int(payload["id"])}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Ogiltig sidmarkör") from exc


def _load_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").casefold() in {"1", "true", "yes", "on"}
