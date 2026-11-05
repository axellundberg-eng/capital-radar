"""Nyhetssteg för strukturella händelser (se events.py): samma kostnadsvakter som finansieringssteget.

Bara nyheter efter rapportens periodslut, rubrikförfilter, nyaste först, högst `max_items`
AI-anrop per bolag, gemensam tokenbudget och aldrig en ny bedömning av redan bedömda nyheter
(`news.events_status`).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .batch import Budget
from .events import StructuralEvent, ask, looks_structural
from .extract import ChatClient
from .fetch import fetch_text
from .financing import build_message
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _set(connection: sqlite3.Connection, document_id: int, **fields) -> None:
    columns = ", ".join(f"{name}=?" for name in fields)
    with connection:
        connection.execute(f"UPDATE news SET {columns}, updated_at=? WHERE document_id=?",
                           (*fields.values(), _now(), document_id))


def _store(connection: sqlite3.Connection, item: sqlite3.Row, events: list[StructuralEvent], tokens: int) -> str:
    status = "structural" if events else "checked"
    with connection:
        connection.execute("DELETE FROM company_events WHERE news_document_id=?", (item["document_id"],))
        connection.executemany(
            """INSERT INTO company_events(news_document_id, company_id, type, status, event_date,
               counterparty, quote, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            [(item["document_id"], item["company_id"], e.type, e.status, e.date, e.counterparty, e.quote, _now())
             for e in events])
    _set(connection, item["document_id"], events_status=status, events_error=None, events_tokens=tokens)
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
    """Aldrig bedömda nyheter efter datumet vars rubrik klarar förfiltret, nyaste först."""
    states = "events_status IS NULL OR events_status='failed'" if retry_failed else "events_status IS NULL"
    rows = connection.execute(
        f"""SELECT * FROM news WHERE company_id=? AND substr(published_at, 1, 10) > ? AND url IS NOT NULL
            AND ({states}) ORDER BY published_at DESC, document_id DESC""",
        (company_id, after_date[:10])).fetchall()
    return [row for row in rows if looks_structural(row["title"])]


def process_company_events(connection: sqlite3.Connection, settings: Settings, client: ChatClient,
                           company_id: int, after_date: str, max_items: int = 8, *,
                           budget: Budget | None = None, retry_failed: bool = False) -> dict[str, int]:
    """Nyaste först, högst `max_items` AI-granskade nyheter. Fel stoppar bara den nyheten."""
    counts = {"events_checked": 0, "structural": 0, "events_failed": 0, "events_tokens": 0}
    for item in candidates(connection, company_id, after_date, retry_failed=retry_failed)[:max_items]:
        if budget is not None and budget.exhausted():
            counts["budget_slut"] = 1
            break
        try:
            status, tokens = _check(connection, settings, client, item)
        except Exception as error:  # noqa: BLE001 - felet sparas och visas
            _set(connection, item["document_id"], events_status="failed", events_error=str(error)[:500])
            counts["events_failed"] += 1
            continue
        counts["structural" if status == "structural" else "events_checked"] += 1
        counts["events_tokens"] += tokens
        if budget is not None:
            budget.add(tokens)
    return counts
