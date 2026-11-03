"""Skrivskyddad bolagsdetalj för webbens bolagsvy (från gamla DashboardRepository)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _load_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class IssuerRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def issuer(self, issuer_id: int) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            issuer = connection.execute(
                """
                SELECT id, canonical_name, jurisdiction_code, lei, org_number,
                       identity_status
                FROM issuers WHERE id=?
                """,
                (issuer_id,),
            ).fetchone()
            if not issuer:
                return None
            securities = connection.execute(
                """
                SELECT symbol, isin, currency, market, segment, mic
                FROM securities
                WHERE issuer_id=? AND active=1
                ORDER BY market, symbol
                """,
                (issuer_id,),
            ).fetchall()
            documents = connection.execute(
                """
                SELECT d.id, d.title, d.published_at, d.cash_relevance_score,
                       d.relevance_reason, d.profile_category,
                       d.analysis_status, d.canonical_url,
                       c.page_count, c.char_count
                FROM documents d
                LEFT JOIN document_contents c ON c.id=(
                    SELECT c2.id FROM document_contents c2
                    WHERE c2.document_id=d.id
                    ORDER BY CASE WHEN c2.status='extracted' THEN 0 ELSE 1 END,
                             c2.id DESC LIMIT 1
                )
                WHERE d.issuer_id=?
                ORDER BY d.published_at DESC
                LIMIT 30
                """,
                (issuer_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT event_type, description, announcement_date,
                       effective_date, status, cash_direction,
                       cash_effect_amount, currency, liquidity_effect_kind,
                       committed_amount, conditional_amount, confidence
                FROM financing_events
                WHERE issuer_id=?
                ORDER BY COALESCE(effective_date, announcement_date) DESC
                """,
                (issuer_id,),
            ).fetchall()
            runway = connection.execute(
                """
                SELECT * FROM runway_assessments
                WHERE issuer_id=? ORDER BY id DESC LIMIT 1
                """,
                (issuer_id,),
            ).fetchone()
            interpretation = connection.execute(
                """
                SELECT * FROM capital_need_interpretations
                WHERE issuer_id=? ORDER BY id DESC LIMIT 1
                """,
                (issuer_id,),
            ).fetchone()
            override = connection.execute(
                """
                SELECT adjusted_runway_months, adjusted_need_score,
                       effective_need_score, effective_alert_level,
                       interpretation_headline, need_timing, alert_level
                FROM issuer_analysis_summary WHERE issuer_id=?
                """,
                (issuer_id,),
            ).fetchone()
            watchlist = connection.execute(
                """
                SELECT active, added_at, updated_at, last_mfn_refresh_at,
                       last_interpretation_at
                FROM issuer_watchlist WHERE issuer_id=?
                """,
                (issuer_id,),
            ).fetchone()
            action_runs = connection.execute(
                """
                SELECT id, action_type, status, phase, deadline_at,
                       started_at, updated_at, completed_at, result_json, error
                FROM manual_action_runs
                WHERE issuer_id=? ORDER BY id DESC LIMIT 8
                """,
                (issuer_id,),
            ).fetchall()
            financial_facts = connection.execute(
                """SELECT id, document_id, fact_type, normalized_value, currency,
                          source_page, source_excerpt, confidence, period_start,
                          period_end, as_of_date, evidence_label
                   FROM financial_facts WHERE issuer_id=?
                   ORDER BY COALESCE(period_end, as_of_date) DESC, id DESC""",
                (issuer_id,),
            ).fetchall()
            report_history = connection.execute(
                """SELECT r.document_id, r.report_period_start, r.report_period_end,
                          r.publication_date, r.currency, r.period_months,
                          r.confidence, r.requires_human_review,
                          d.title, d.canonical_url
                   FROM report_extractions r JOIN documents d ON d.id=r.document_id
                   WHERE r.issuer_id=? AND r.is_financial_report=1
                   ORDER BY r.report_period_end DESC, r.document_id DESC""",
                (issuer_id,),
            ).fetchall()
        interpretation_payload = dict(interpretation) if interpretation else None
        if interpretation_payload:
            interpretation_payload["evidence"] = _load_json(
                interpretation_payload.pop("evidence_json"), []
            )
            interpretation_payload["source_document_ids"] = _load_json(
                interpretation_payload.pop("source_document_ids_json"), []
            )
        action_payloads = []
        for row in action_runs:
            action = dict(row)
            result = _load_json(action.pop("result_json"), {})
            action["reason"] = (
                result.get("reason") if isinstance(result, dict) else None
            )
            action_payloads.append(action)
        runway_dict = dict(runway) if runway else None
        if override and runway_dict:
            ov = dict(override)
            if ov.get("adjusted_runway_months") is not None:
                runway_dict["adjusted_runway_months"] = ov["adjusted_runway_months"]
            if ov.get("adjusted_need_score") is not None:
                runway_dict["adjusted_need_score"] = ov["adjusted_need_score"]
            if ov.get("need_timing") is not None:
                runway_dict["need_timing"] = ov["need_timing"]
            if ov.get("alert_level") is not None:
                runway_dict["alert_level"] = ov["alert_level"]
        if override and interpretation_payload:
            ov = dict(override)
            if ov.get("interpretation_headline") is not None:
                interpretation_payload["headline"] = ov["interpretation_headline"]
        return {
            "issuer": dict(issuer),
            "securities": [dict(row) for row in securities],
            "documents": [dict(row) for row in documents],
            "financing_events": [dict(row) for row in events],
            "financial_facts": [dict(row) for row in financial_facts],
            "report_history": [dict(row) for row in report_history],
            "runway": runway_dict,
            "interpretation": interpretation_payload,
            "watchlist": dict(watchlist) if watchlist else {"active": 0},
            "action_runs": action_payloads,
        }


