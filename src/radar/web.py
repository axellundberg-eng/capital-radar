"""Webb-API och gränssnitt för den nya kärnan.

    python -m radar serve

Läser bara den nya databasen. Inga skrivningar sker via webben.
"""

from __future__ import annotations

import json
import sqlite3
from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import connect
from .qualify import load_history, load_market, qualify
from .settings import Settings

BANDS = (("under_3", 0, 3), ("3_9", 3, 9), ("9_18", 9, 18), ("over_18", 18, None))
FLAG_TEXT = {
    "stale_report": "Senaste rapporten är äldre än 7 månader",
    "recent_period_burn_higher": "Senaste perioden bränner snabbare än snittet",
    "investing_as_capex": "Investeringar uppskattade från investeringsflödet",
    "includes_short_term_investments": "Likviditeten inkluderar kortfristiga placeringar",
    "cash_exhausted_since_report": "Kassan borde vara slut sedan rapporten: kontrollera ny finansiering",
    "raise_announced": "En emission eller finansiering är annonserad men inte genomförd",
    "post_period_outflows": "Utbetalningar efter perioden (t.ex. amortering) är avdragna från kassan",
    "debt_due_within_12m": "Lån förfaller inom 12 månader: runway efter amortering är klart kortare",
    "financing_received": "Genomförd finansiering efter rapporten ingår i runway",
}


def band(row: dict) -> str:
    if row["status"] != "ok":
        return "missing"
    if row["cash_flow_positive"]:
        return "positive"
    value = row["runway_today"]
    refinanced = "financing_received" in row["flags"] and (value or 0) > 0
    if "cash_exhausted_since_report" in row["flags"] and not refinanced:
        return "exhausted"
    for name, low, high in BANDS:
        if value >= low and (high is None or value < high):
            return name
    return "missing"


def latest_rows(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """SELECT c.id, c.name, c.market, c.segment, r.document_id, r.title, r.published_at, rr.result_json
           FROM companies c
           JOIN reports r ON r.company_id = c.id
           JOIN runway_results rr ON rr.document_id = r.document_id
           WHERE r.document_id = (SELECT r2.document_id FROM reports r2
                                  JOIN runway_results x ON x.document_id = r2.document_id
                                  WHERE r2.company_id = c.id
                                  ORDER BY r2.published_at DESC, r2.document_id ASC LIMIT 1)""").fetchall()
    items = []
    for row in rows:
        result = json.loads(row["result_json"])
        item = {
            "id": row["id"], "name": row["name"], "market": row["market"], "segment": row["segment"],
            "document_id": row["document_id"], "report_title": row["title"],
            "published_at": row["published_at"], "status": result.get("status"),
            "runway_today": result.get("runway_today"), "runway_at_report": result.get("runway_at_report"),
            "monthly_burn": result.get("monthly_burn"), "cash": result.get("cash"),
            "cash_flow_positive": result.get("cash_flow_positive", False),
            "months_since_report": result.get("months_since_report"),
            "period_end": result.get("period_end"), "flags": result.get("flags", []),
            "recent_monthly_burn": result.get("recent_monthly_burn"),
            "recent_runway_today": result.get("recent_runway_today"),
            "confirmed_financing": result.get("confirmed_financing", 0.0),
            "announced_financing": result.get("announced_financing", 0.0),
            "financing_events": result.get("financing_events", []),
            "post_period_outflows": result.get("post_period_outflows", 0.0),
            "reported_inflows_after_period": result.get("reported_inflows_after_period", 0.0),
            "debt_due_12m": result.get("debt_due_12m"),
            "runway_after_debt": result.get("runway_after_debt"),
        }
        item["band"] = band(item)
        items.append(item)
    return items


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Capital Radar", version=__version__)
    static = resources.files("radar") / "static"

    def db() -> sqlite3.Connection:
        return connect(settings.db_path)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/overview")
    def overview() -> JSONResponse:
        connection = db()
        try:
            items = latest_rows(connection)
            reports = dict(connection.execute("SELECT status, COUNT(*) FROM reports GROUP BY status").fetchall())
            companies = connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        finally:
            connection.close()
        counts: dict[str, int] = {}
        for item in items:
            counts[item["band"]] = counts.get(item["band"], 0) + 1
        return JSONResponse({"companies": companies, "with_result": len(items), "bands": counts,
                             "reports": reports, "flag_text": FLAG_TEXT},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/companies")
    def companies() -> JSONResponse:
        connection = db()
        try:
            items = latest_rows(connection)
        finally:
            connection.close()
        items.sort(key=lambda i: (i["runway_today"] is None, i["runway_today"] or 0, i["name"].casefold()))
        return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})

    @app.get("/api/companies/{company_id}")
    def company(company_id: int) -> JSONResponse:
        connection = db()
        try:
            item = next((i for i in latest_rows(connection) if i["id"] == company_id), None)
            if item is None:
                raise HTTPException(404, "Bolaget saknar beräknad runway")
            report = connection.execute("SELECT pdf_url, page_count FROM reports WHERE document_id=?",
                                        (item["document_id"],)).fetchone()
            result = json.loads(connection.execute(
                "SELECT result_json FROM runway_results WHERE document_id=?", (item["document_id"],)).fetchone()[0])
            facts = [dict(row) for row in connection.execute(
                """SELECT kind, value, currency, period_start, period_end, page, quote FROM facts
                   WHERE extraction_id = (SELECT MAX(id) FROM extractions WHERE document_id=? AND status='ok')
                   ORDER BY kind, period_start""", (item["document_id"],))]
            market = load_market(connection).get(company_id)
            history = load_history(connection).get(company_id, [])
        finally:
            connection.close()
        return JSONResponse({**item, "pdf_url": report["pdf_url"], "page_count": report["page_count"],
                             "periods": result.get("periods", []), "facts": facts,
                             "qualification": qualify(item, market, history).to_dict(),
                             "flag_text": {flag: FLAG_TEXT.get(flag, flag) for flag in item["flags"]}},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/prospects")
    def prospects() -> JSONResponse:
        connection = db()
        try:
            items = latest_rows(connection)
            market, history = load_market(connection), load_history(connection)
        finally:
            connection.close()
        out = []
        for item in items:
            if item["band"] not in ("3_9", "under_3") or "raise_announced" in item["flags"]:
                continue
            q = qualify(item, market.get(item["id"]), history.get(item["id"], []))
            out.append({**item, **q.to_dict(), "market_error": (market.get(item["id"]) or {}).get("error")})
        out.sort(key=lambda i: (-i["score"], i["runway_today"] or 0, i["name"].casefold()))
        return JSONResponse({"items": out}, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(static / "index.html"), headers={"Cache-Control": "no-store"})

    return app
