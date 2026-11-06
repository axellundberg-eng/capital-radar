"""Bemyndiganden för styrelsen att emittera aktier, utlästa ur stämmokommunikéer.

Enbart reguljära uttryck, inga AI-anrop. Kandidater är nyheter vars rubrik ser ut som
en kommuniké från stämma (kallelser hoppas över) de senaste 15 månaderna och som
ännu inte finns i `authorizations`. Text hämtas bara om nyheten saknar sparad text.
Granskade nyheter utan bemyndigande markeras inte; `limit` begränsar hämtningarna per varv.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import mfn
from .calendar import parse_event_date
from .fetch import fetch_text
from .settings import Settings

MONTHS_BACK = 15
QUOTE_CHARS = 300
WINDOW_CHARS = 700

NOTICE = re.compile(r"kallelse|notice|invitation|inbjudan|call to|calling|summons|förslag till beslut", re.I)
RESULT = re.compile(r"kommuniké|communiqu[eé]|bulletin|resolutions?|beslut|decisions?|outcome|hölls|held", re.I)
MEETING = re.compile(r"årsstämma|bolagsstämma|extra stämma|general meeting|\bagm\b|\begm\b", re.I)

ANCHOR = re.compile(
    r"bemyndig\w*[^.]{0,250}?(?:nyemission|emission|emittera|nya aktier|konvertibler|teckningsoption)"
    r"|authori[sz]\w*[^.]{0,250}?\bissu\w*[^.]{0,200}?(?:shares|warrants|convertible)"
    r"|authori[sz]\w*[^.]{0,120}?new (?:shares|warrants)", re.I)
PERCENT = re.compile(
    r"(?:högst|upp till|maximalt|max\.?|not exceeding|up to|a maximum of|maximum of|at most|corresponding to)"
    r"\s+(?:ca\.?\s+|cirka\s+|approximately\s+)?(\d+(?:[.,]\d+)?)\s*(?:procent|%|per ?cent)", re.I)
PERCENT_OF = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:procent|%|per ?cent)\s+(?:av|of)\b", re.I)
SHARES = re.compile(
    r"(?:högst|upp till|maximalt|not exceeding|up to|a maximum of|maximum of)\s+(\d[\d\s,.]*\d|\d)"
    r"(?:\s*(miljoner|miljon|million|mn))?\s*(?:st\.?\s+)?(?:nya\s+|new\s+)?"
    r"(?:aktier|shares|teckningsoptioner|warrants|units)", re.I)
DIRECTED = re.compile(
    r"avvik\w+\s+från\s+(?:aktieägarnas\s+)?företrädesrätt|med\s+eller\s+utan\s+(?:avvikelse\s+från\s+)?"
    r"(?:aktieägarnas\s+)?företrädesrätt|deviat\w+\s+from\s+(?:the\s+)?shareholders['’]?\s+"
    r"(?:pre-?emptive|preferential)|with\s+or\s+without\s+(?:deviation\s+from\s+)?(?:the\s+)?"
    r"(?:shareholders['’]?\s+)?(?:preferential|pre-?emptive)|disapply\w*\s+(?:the\s+)?"
    r"(?:shareholders['’]?\s+)?(?:preferential|pre-?emptive)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_resolution_title(title: str | None) -> bool:
    """Kommuniké/utfall från stämma, inte kallelse."""
    text = title or ""
    return bool(MEETING.search(text) and RESULT.search(text) and not NOTICE.search(text))


def _number(raw: str) -> float | None:
    digits = re.sub(r"[\s ,.]", "", raw)
    return float(digits) if digits.isdigit() else None


def parse_authorization(text: str, published_at: str | None = None) -> dict | None:
    """Ett bemyndigande att emittera aktier ur stämmotexten, annars None."""
    flat = " ".join((text or "").split())
    match = ANCHOR.search(flat)
    if not match:
        return None
    window = flat[match.start(): match.start() + WINDOW_CHARS]
    percent = PERCENT.search(window) or PERCENT_OF.search(window)
    shares = SHARES.search(window)
    max_shares = None
    if shares:
        if shares[2]:
            max_shares = float(shares[1].replace(",", ".").replace(" ", "")) * 1_000_000
        else:
            max_shares = _number(shares[1])
    meeting = parse_event_date(flat[:800], published_at, roll_forward=False) or (published_at or "")[:10] or None
    return {
        "meeting_date": meeting,
        "max_dilution_pct": float(percent[1].replace(",", ".")) if percent else None,
        "max_shares": max_shares,
        "directed_allowed": 1 if DIRECTED.search(window) else 0,
        "quote": flat[match.start(): match.start() + QUOTE_CHARS],
    }


def candidates(connection: sqlite3.Connection, today: str, limit: int) -> list[sqlite3.Row]:
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=MONTHS_BACK * 30 + 2)).isoformat()
    rows = connection.execute(
        """SELECT n.document_id, n.company_id, n.title, n.published_at, n.url, n.text FROM news n
           LEFT JOIN authorizations a ON a.news_document_id = n.document_id
           WHERE a.id IS NULL AND substr(n.published_at, 1, 10) >= ?
           ORDER BY (n.text IS NOT NULL), n.published_at DESC, n.document_id DESC""", (cutoff,)).fetchall()
    return [row for row in rows if is_resolution_title(row["title"])][:limit]


def run(connection: sqlite3.Connection, settings: Settings, *, limit: int = 40,
        log: Callable[[str], None] = print, today: str | None = None,
        throttle: mfn.Throttle | None = None) -> dict[str, int]:
    """Läs upp till `limit` stämmokommunikéer och spara funna bemyndiganden."""
    throttle = throttle or mfn.Throttle()
    rows = candidates(connection, today or date.today().isoformat(), limit)
    counts = {"candidates": len(rows), "fetched": 0, "found": 0, "failed": 0}
    for row in rows:
        text = row["text"]
        if not text:
            if not row["url"]:
                continue
            throttle.wait()
            try:
                text = fetch_text(row["url"], timeout=min(settings.http_timeout, 60.0),
                                  user_agent=settings.user_agent)
            except Exception as error:  # noqa: BLE001 - en sida får inte stoppa övriga
                counts["failed"] += 1
                log(f"authorizations: {row['title'][:60]}: {error}")
                continue
            counts["fetched"] += 1
            # Spara texten så att sidan bara hämtas en gång, även om inget bemyndigande hittas.
            with connection:
                connection.execute("UPDATE news SET text=? WHERE document_id=? AND text IS NULL",
                                   (text, row["document_id"]))
        found = parse_authorization(text, row["published_at"])
        if not found:
            continue
        with connection:
            connection.execute(
                """INSERT OR IGNORE INTO authorizations(company_id, news_document_id, meeting_date,
                   max_dilution_pct, max_shares, directed_allowed, quote, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (row["company_id"], row["document_id"], found["meeting_date"], found["max_dilution_pct"],
                 found["max_shares"], found["directed_allowed"], found["quote"], _now()))
        counts["found"] += 1
    log(f"authorizations: {counts}")
    return counts


def latest(connection: sqlite3.Connection) -> dict[int, dict]:
    """Det senaste bemyndigandet per bolag."""
    result: dict[int, dict] = {}
    for row in connection.execute(
            "SELECT * FROM authorizations ORDER BY COALESCE(meeting_date, '') DESC, id DESC"):
        result.setdefault(row["company_id"], dict(row))
    return result
