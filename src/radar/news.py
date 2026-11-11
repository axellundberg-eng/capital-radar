"""Steg 4: läs pressmeddelanden efter senaste rapporten och hitta finansiering.

Kostnadsvakter: bara bolag med kort runway, bara nyheter efter rapportens
periodslut, förfilter på rubriken, högst `max_items` AI-anrop per bolag,
texten klipps till 12 000 tecken, redan bedömda nyheter hoppas över och en
gemensam tokenbudget stoppar nya anrop när den är förbrukad.

Bakåtgranskning (`run_backfill`): finansieringsnyheter de senaste 24 månaderna, även före rapportens
periodslut, så att emissionshistoriken (belopp, villkor, garanter, teckningsgrad) blir komplett. Bolag i
målgruppen granskas först. Egen tokenbudget, så att den löpande granskningen inte trängs undan.

Tvillingar: MFN publicerar ofta samma pressmeddelande på svenska och engelska samma dag. Den engelska
versionen hoppas över (status `twin`) när en svensk finns samma dag; finns bara en engelsk läses den.

Samma körning söker också strukturella händelser (omvänt förvärv, fusion, bud m.fl.,
se events.py och news_events.py) för bolag med runway under `event_runway` månader
eller kassa slut enligt rapporten, med samma budget och samma kostnadsvakter.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .batch import Budget
from .db import connect
from .extract import ChatClient
from .fetch import fetch_text
from .financing import TERM_FIELDS, ask, build_message, looks_like_financing
from .news_events import process_company_events
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
SIGNALS = ("raise_announced", "financing_received", "structural_change", "public_offer", "delisting")
EVENT_RUNWAY = 18.0   # strukturell kontroll för bolag med runway under så många månader
BACKFILL_MONTHS = 24
TARGET_RUNWAY = 18.0   # bakåtgranskningens första prioritet: runway under så många månader (eller kassa slut)
ENGLISH = re.compile(r"\b(?:the|of|and|announces?|issue|shares|rights|directed|warrants|loan|board|resolves?|outcome"
                     r"|subscription|completes?|completed|intends|carries|period|units|convertible)\b", re.I)
# Svenska: ett ord med små bokstäver som innehåller å, ä eller ö (bolagsnamn skrivs med stor bokstav,
# t.ex. "Mål AB announces ..."), eller ett typiskt svenskt ord.
SWEDISH_WORD = re.compile(r"\b[a-zåäö]*[åäö][a-zåäö]*\b")
SWEDISH = re.compile(r"\b(?:och|av|för|om|genomför|nyemission|företrädesemission|beslutar|utfall|teckning"
                     r"|lån|riktad|inleds|sista)\b", re.I)


def is_english(title: str | None) -> bool:
    text = title or ""
    return bool(ENGLISH.search(text)) and not SWEDISH.search(text) and not SWEDISH_WORD.search(text)


def split_twins(rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(att läsa, engelska tvillingar): engelska rubriker hoppas över när en svensk finns samma bolag och dag."""
    swedish_days = {(r["company_id"], (r["published_at"] or "")[:10]) for r in rows if not is_english(r["title"])}
    keep, twins = [], []
    for row in rows:
        day = (row["company_id"], (row["published_at"] or "")[:10])
        (twins if is_english(row["title"]) and day in swedish_days else keep).append(row)
    return keep, twins


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
            f"""INSERT INTO financing_events(news_document_id, company_id, type, status, gross_amount,
               net_amount, currency, event_date, quote, created_at, drawn_amount, advisers, {", ".join(TERM_FIELDS)})
               VALUES ({", ".join("?" * (12 + len(TERM_FIELDS)))})""",
            [(item["document_id"], item["company_id"], e.type, e.status, e.gross_amount, e.net_amount,
              e.currency, e.date, e.quote, _now(), e.drawn_amount, e.advisers,
              *(getattr(e, key) for key in TERM_FIELDS)) for e in events])
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
    keep, twins = split_twins(candidates(connection, company_id, after_date, retry_failed=retry_failed))
    for twin in twins:   # samma pressmeddelande som den svenska versionen: inget eget AI-anrop
        _set(connection, twin["document_id"], status="twin", error="engelsk version av en svensk nyhet samma dag")
    counts["twins"] = len(twins)
    for item in keep[:max_items]:
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


def backfill_targets(connection: sqlite3.Connection) -> list[tuple[int, int | None, int]]:
    """(bolag, senaste rapport eller None, prioritet) för alla bolag med MFN-nyheter, viktigast först.

    Prioritet 0: målgruppen (runway under TARGET_RUNWAY, kassa slut, pågående kapitalanskaffning eller
    strukturell förändring). 1: övriga som bränner kassa. 2: kassaflödespositiva och bolag utan runway."""
    latest = {row["id"]: (row["document_id"], json.loads(row["result_json"])) for row in connection.execute(LATEST_RESULTS)}
    targets = []
    for (company_id,) in connection.execute("SELECT DISTINCT company_id FROM news"):
        document_id, result = latest.get(company_id, (None, {}))
        flags = result.get("flags", [])
        if result.get("status") != "ok" or result.get("cash_flow_positive"):
            priority = 2
        elif ((result.get("runway_today") or 0) < TARGET_RUNWAY or "cash_exhausted_since_report" in flags
              or any(flag in flags for flag in SIGNALS)):
            priority = 0
        else:
            priority = 1
        targets.append((company_id, document_id, priority))
    return sorted(targets, key=lambda t: (t[2], t[0]))


def run_backfill(settings: Settings, client: ChatClient, *, today: str, months: int = BACKFILL_MONTHS,
                 token_budget: int = 100_000, workers: int = 4, max_items: int = 60, max_priority: int = 2,
                 log: Callable[[str], None] = print) -> dict[str, int]:
    """Granska ogranskade finansieringsnyheter de senaste `months` månaderna, målgruppen först.

    Nyheter före rapportens periodslut ändrar inte runway (den räknar bara händelser efter periodslut) men
    fyller emissionshistoriken, Garanti-sidan och markörer som "nyligen finansierad"."""
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=round(months * 30.44))).isoformat()
    main = connect(settings.db_path)
    try:
        jobs = [job for job in backfill_targets(main) if job[2] <= max_priority
                and any(looks_like_financing(row["title"]) for row in candidates(main, job[0], cutoff))]
    finally:
        main.close()
    budget, totals, lock = Budget(token_budget, 0), {"companies": len(jobs)}, threading.Lock()

    def work(company_id: int, document_id: int | None, priority: int) -> dict:
        if budget.exhausted():
            return {"budget_slut": 1}
        connection = connect(settings.db_path)
        try:
            counts = process_company_news(connection, settings, client, company_id, cutoff, max_items, budget=budget)
            if document_id is not None and counts["financing"]:
                refresh_runway(connection, document_id, today=today)
        finally:
            connection.close()
        if counts["checked"] or counts["financing"] or counts["failed"]:
            log(f"bakåt p{priority}: {counts['checked'] + counts['financing']} granskade, {counts['financing']} med "
                f"finansiering, {counts['twins']} tvillingar, {counts['failed']} fel  bolag {company_id}")
        return counts

    # Prioriteterna körs i ordning så att budgeten går till målgruppen först.
    for level in range(max_priority + 1):
        batch = [job for job in jobs if job[2] == level]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for future in as_completed(pool.submit(work, *job) for job in batch):
                with lock:
                    for key, value in future.result().items():
                        totals[key] = totals.get(key, 0) + value
        if budget.exhausted():
            break
    totals["tokens_used"] = totals.pop("tokens", 0)
    totals["budget_slut"] = 1 if budget.exhausted() or totals.get("budget_slut") else 0
    return totals


def news_targets(connection: sqlite3.Connection, max_runway: float, event_runway: float = EVENT_RUNWAY
                 ) -> list[tuple[int, str, int, str, bool]]:
    """(bolag, namn, rapport, periodslut, finansieringssteg) för bolag som bränner kassa.

    Finansieringsnyheter läses när runway är under `max_runway`; strukturella händelser
    när den är under `event_runway`. Kassa slut enligt rapporten räknas som kort i båda."""
    targets = []
    for row in connection.execute(LATEST_RESULTS):
        result = json.loads(row["result_json"])
        if result.get("status") != "ok" or result.get("cash_flow_positive") or not result.get("period_end"):
            continue
        runway = result.get("runway_today") or 0
        exhausted = "cash_exhausted_since_report" in result.get("flags", [])
        if runway < max(max_runway, event_runway) or exhausted:
            targets.append((row["id"], row["name"], row["document_id"], result["period_end"],
                            runway < max_runway or exhausted))
    return targets


def run_news(settings: Settings, client: ChatClient, *, today: str, max_runway: float = 12.0,
             workers: int = 4, token_budget: int = 1_000_000, max_items: int = 8,
             retry_failed: bool = False, event_runway: float = EVENT_RUNWAY,
             log: Callable[[str], None] = print) -> dict[str, int]:
    main = connect(settings.db_path)
    try:
        jobs = news_targets(main, max_runway, event_runway)
    finally:
        main.close()
    budget, totals, lock = Budget(token_budget, 0), {"companies": len(jobs)}, threading.Lock()

    def work(company_id: int, name: str, document_id: int, period_end: str, financing: bool) -> dict:
        connection = connect(settings.db_path)
        try:
            counts = {"checked": 0, "financing": 0, "failed": 0, "tokens": 0}
            if financing:
                counts = process_company_news(connection, settings, client, company_id, period_end, max_items,
                                              budget=budget, retry_failed=retry_failed)
            found = process_company_events(connection, settings, client, company_id, period_end, max_items,
                                           budget=budget, retry_failed=retry_failed)
            counts["tokens"] += found.pop("events_tokens")
            counts.update({key: counts.get(key, 0) + value for key, value in found.items()})
            result = refresh_runway(connection, document_id, today=today)
        finally:
            connection.close()
        signals = [flag for flag in result.get("flags", []) if flag in SIGNALS]
        log(f"{counts['checked'] + counts['financing'] + counts['failed']:2} nyheter, "
            f"{counts['financing']:2} med finansiering, {counts['failed']:2} fel, "
            f"{counts['structural']:2} strukturella  "
            f"runway {result.get('runway_today')}  {','.join(signals):28} {name}")
        return {**counts, **{flag: 1 for flag in signals}}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed(pool.submit(work, *job) for job in jobs):
            with lock:
                for key, value in future.result().items():
                    totals[key] = totals.get(key, 0) + value
    totals["tokens_used"] = totals.pop("tokens", 0)
    return totals
