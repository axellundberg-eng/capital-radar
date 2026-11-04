"""Steg 4: läs pressmeddelanden efter senaste rapporten och hitta finansiering.

Kostnadsvakter: bara bolag med kort runway, bara nyheter efter rapportens
periodslut, förfilter på rubriken, högst `max_items` AI-anrop per bolag,
texten klipps till 12 000 tecken, redan bedömda nyheter hoppas över och en
gemensam tokenbudget stoppar nya anrop när den är förbrukad.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .batch import Budget
from .db import connect
from .extract import ChatClient
from .fetch import fetch_text
from .financing import ask, build_message, looks_like_financing
from .pipeline import refresh_runway
from .settings import Settings

LATEST_RESULTS = """
SELECT c.id, c.name, r.document_id, rr.result_json FROM companies c
JOIN reports r ON r.company_id = c.id JOIN runway_results rr ON rr.document_id = r.document_id
WHERE r.document_id = (SELECT r2.document_id FROM reports r2
                       JOIN runway_results x ON x.document_id = r2.document_id
                       WHERE r2.company_id = c.id ORDER BY r2.published_at DESC, r2.document_id ASC LIMIT 1)
ORDER BY c.name COLLATE NOCASE
"""
SIGNALS = ("raise_announced", "financing_received")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set(connection: sqlite3.Connection, document_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    columns = ", ".join(f"{name}=?" for name in fields)
    with connection:
        connection.execute(f"UPDATE news SET {columns} WHERE document_id=?", (*fields.values(), document_id))


def _store(connection: sqlite3.Connection, item: sqlite3.Row, events: list, tokens: int) -> str:
    status = "financing" if events else "checked"
    with connection:
        connection.execute("DELETE FROM financing_events WHERE news_document_id=?", (item["document_id"],))
        connection.executemany(
            """INSERT INTO financing_events(news_document_id, company_id, type, status, gross_amount,
               net_amount, currency, event_date, quote, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(item["document_id"], item["company_id"], e.type, e.status, e.gross_amount, e.net_amount,
              e.currency, e.date, e.quote, _now()) for e in events])
    _set(connection, item["document_id"], status=status, error=None, tokens=tokens)
    return status


def _check(connection, settings: Settings, client: ChatClient, item: sqlite3.Row) -> tuple[str, int]:
    text = item["text"]
    if not text:
        text = fetch_text(item["url"], timeout=settings.http_timeout, user_agent=settings.user_agent)
        _set(connection, item["document_id"], text=text)
    events, usage = ask(client, build_message(item["title"], item["published_at"], text), item["published_at"])
    tokens = usage["prompt_tokens"] + usage["completion_tokens"]
    return _store(connection, item, events, tokens), tokens


def candidates(connection: sqlite3.Connection, company_id: int, after_date: str, *,
               retry_failed: bool = False) -> list[sqlite3.Row]:
    """Obehandlade nyheter efter datumet vars rubrik klarar förfiltret, nyaste först."""
    statuses = ("new", "failed") if retry_failed else ("new",)
    rows = connection.execute(
        f"""SELECT * FROM news WHERE company_id=? AND substr(published_at, 1, 10) > ? AND url IS NOT NULL
            AND status IN ({','.join('?' * len(statuses))}) ORDER BY published_at DESC, document_id DESC""",
        (company_id, after_date[:10], *statuses)).fetchall()
    return [row for row in rows if looks_like_financing(row["title"])]


def process_company_news(connection: sqlite3.Connection, settings: Settings, client: ChatClient,
                         company_id: int, after_date: str, max_items: int = 8, *,
                         budget: Budget | None = None, retry_failed: bool = False) -> dict[str, int]:
    """Nyaste först, högst `max_items` AI-granskade nyheter. Fel stoppar bara den nyheten."""
    counts = {"checked": 0, "financing": 0, "failed": 0, "tokens": 0}
    for item in candidates(connection, company_id, after_date, retry_failed=retry_failed)[:max_items]:
        if budget is not None and budget.exhausted():
            counts["budget_slut"] = 1
            break
        try:
            status, tokens = _check(connection, settings, client, item)
        except Exception as error:  # noqa: BLE001 - felet sparas och visas
            _set(connection, item["document_id"], status="failed", error=str(error)[:500])
            counts["failed"] += 1
            continue
        counts[status] += 1
        counts["tokens"] += tokens
        if budget is not None:
            budget.add(tokens)
    return counts


def news_targets(connection: sqlite3.Connection, max_runway: float) -> list[tuple[int, str, int, str]]:
    """(bolag, namn, rapport, periodslut) för bolag med kort runway som bränner kassa."""
    targets = []
    for row in connection.execute(LATEST_RESULTS):
        result = json.loads(row["result_json"])
        if result.get("status") != "ok" or result.get("cash_flow_positive") or not result.get("period_end"):
            continue
        short = (result.get("runway_today") or 0) < max_runway
        if short or "cash_exhausted_since_report" in result.get("flags", []):
            targets.append((row["id"], row["name"], row["document_id"], result["period_end"]))
    return targets


def run_news(settings: Settings, client: ChatClient, *, today: str, max_runway: float = 12.0,
             workers: int = 4, token_budget: int = 1_000_000, max_items: int = 8,
             retry_failed: bool = False, log: Callable[[str], None] = print) -> dict[str, int]:
    main = connect(settings.db_path)
    try:
        jobs = news_targets(main, max_runway)
    finally:
        main.close()
    budget, totals, lock = Budget(token_budget, 0), {"companies": len(jobs)}, threading.Lock()

    def work(company_id: int, name: str, document_id: int, period_end: str) -> dict:
        connection = connect(settings.db_path)
        try:
            counts = process_company_news(connection, settings, client, company_id, period_end, max_items,
                                          budget=budget, retry_failed=retry_failed)
            result = refresh_runway(connection, document_id, today=today)
        finally:
            connection.close()
        signals = [flag for flag in result.get("flags", []) if flag in SIGNALS]
        log(f"{counts['checked'] + counts['financing'] + counts['failed']:2} nyheter, "
            f"{counts['financing']:2} med finansiering, {counts['failed']:2} fel  "
            f"runway {result.get('runway_today')}  {','.join(signals):28} {name}")
        return {**counts, **{flag: 1 for flag in signals}}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed(pool.submit(work, *job) for job in jobs):
            with lock:
                for key, value in future.result().items():
                    totals[key] = totals.get(key, 0) + value
    totals["tokens_used"] = totals.pop("tokens", 0)
    return totals
