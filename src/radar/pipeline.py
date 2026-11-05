"""Kedjan för en rapport: hämta → text → extrahera → runway.

Varje steg sparar sin status. Ett fel stoppar bara den rapporten och syns
som `failed` med felorsak; nästa körning fortsätter där det stannade.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .events import load_events as load_structural, summarize as summarize_structural
from .extract import ChatClient, ExtractionError, extract
from .fetch import fetch_pdf
from .financing import load_events
from .pdftext import pages as pdf_pages
from .rounds import summarize
from .runway import Fact, compute
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set(connection: sqlite3.Connection, document_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    columns = ", ".join(f"{name}=?" for name in fields)
    with connection:
        connection.execute(f"UPDATE reports SET {columns} WHERE document_id=?", (*fields.values(), document_id))


def _fail(connection: sqlite3.Connection, document_id: int, step: str, error: Exception | str) -> dict:
    message = f"{step}: {error}"[:500]
    _set(connection, document_id, status="failed", error=message)
    return {"document_id": document_id, "status": "failed", "error": message}


def _fetch(connection, settings: Settings, report: sqlite3.Row) -> None:
    if not report["pdf_url"]:
        raise ValueError("PDF-länk saknas")
    target = settings.files_path / "pdf" / f"{report['document_id']}.pdf"
    path, sha = fetch_pdf(report["pdf_url"], target, timeout=settings.http_timeout, user_agent=settings.user_agent)
    _set(connection, report["document_id"], status="fetched", pdf_path=str(path), pdf_sha256=sha, error=None)


def _text(connection, report: sqlite3.Row) -> None:
    texts = pdf_pages(Path(report["pdf_path"]))
    if not any(texts):
        raise ValueError("PDF:en saknar text (troligen skannad)")
    with connection:
        connection.execute("DELETE FROM report_pages WHERE document_id=?", (report["document_id"],))
        connection.executemany("INSERT INTO report_pages(document_id, page, text) VALUES (?,?,?)",
                               [(report["document_id"], n, t) for n, t in enumerate(texts, start=1)])
    _set(connection, report["document_id"], status="text", page_count=len(texts), error=None)


def _extract(connection, client: ChatClient, model: str, report: sqlite3.Row) -> None:
    document_id = report["document_id"]
    texts = [row[0] for row in connection.execute(
        "SELECT text FROM report_pages WHERE document_id=? ORDER BY page", (document_id,))]
    try:
        result, pages, raw, usage = extract(client, texts)
    except ExtractionError as error:
        with connection:
            connection.execute(
                """INSERT INTO extractions(document_id, model, pages, status, error, response_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (document_id, model, "[]", "invalid", str(error), getattr(error, "raw", None), _now()))
        raise
    with connection:
        cursor = connection.execute(
            """INSERT INTO extractions(document_id, model, pages, status, response_json, prompt_tokens,
               completion_tokens, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (document_id, model, json.dumps(pages), "ok", raw, usage.get("prompt_tokens"),
             usage.get("completion_tokens"), _now()))
        connection.executemany(
            """INSERT INTO facts(extraction_id, document_id, company_id, kind, value, currency,
               period_start, period_end, page, quote) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(cursor.lastrowid, document_id, report["company_id"], f.kind, f.value, f.currency,
              f.period_start, f.period_end, f.page, (f.quote or "")[:300]) for f in result.facts])
    _set(connection, document_id, status="extracted", error=None)


def runway_for(connection: sqlite3.Connection, document_id: int, *, today: str) -> dict:
    extraction = connection.execute(
        "SELECT id, response_json FROM extractions WHERE document_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
        (document_id,)).fetchone()
    if extraction is None:
        return {"status": "missing_extraction"}
    period_end = json.loads(extraction["response_json"]).get("report_period_end", "")[:10]
    rows = connection.execute(
        "SELECT kind, value, period_start, period_end, currency FROM facts WHERE extraction_id=?",
        (extraction["id"],)).fetchall()
    facts = [Fact(r["kind"], r["value"], r["period_start"], r["period_end"]) for r in rows]
    company = connection.execute("SELECT company_id FROM reports WHERE document_id=?", (document_id,)).fetchone()
    currency = next((r["currency"] for r in rows if r["currency"]), None)
    financing = summarize(load_events(connection, company["company_id"]), period_end, currency)
    structural = summarize_structural(load_structural(connection, company["company_id"]), period_end)
    return compute(facts, period_end, today=today, financing=financing, structural=structural).to_dict()


def refresh_runway(connection: sqlite3.Connection, document_id: int, *, today: str) -> dict:
    """Räkna om och spara runway för en redan extraherad rapport (inga AI-anrop)."""
    result = runway_for(connection, document_id, today=today)
    if result.get("status") != "missing_extraction":
        company = connection.execute("SELECT company_id FROM reports WHERE document_id=?",
                                     (document_id,)).fetchone()
        with connection:
            connection.execute(
                """INSERT INTO runway_results(company_id, document_id, computed_at, result_json) VALUES (?,?,?,?)
                   ON CONFLICT(company_id, document_id) DO UPDATE SET computed_at=excluded.computed_at,
                   result_json=excluded.result_json""",
                (company["company_id"], document_id, _now(), json.dumps(result)))
    return result


def process(connection: sqlite3.Connection, settings: Settings, client: ChatClient | None,
            document_id: int, *, today: str, model: str = "") -> dict:
    report = connection.execute("SELECT * FROM reports WHERE document_id=?", (document_id,)).fetchone()
    if report is None:
        return {"document_id": document_id, "status": "unknown_document"}
    steps = [("hämta", lambda r: _fetch(connection, settings, r), {"new", "failed"}),
             ("text", lambda r: _text(connection, r), {"fetched"})]
    if client is not None:
        steps.append(("extrahera", lambda r: _extract(connection, client, model, r), {"text"}))
    for name, step, needs in steps:
        report = connection.execute("SELECT * FROM reports WHERE document_id=?", (document_id,)).fetchone()
        if report["status"] in needs:
            try:
                step(report)
            except Exception as error:  # noqa: BLE001 - felet sparas och visas
                return _fail(connection, document_id, name, error)
    result = refresh_runway(connection, document_id, today=today)
    return {"document_id": document_id, "report_status": report["status"], "runway": result}
