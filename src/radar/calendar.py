"""Kalenderposter från MFN: datum för nästa rapport och bolagsstämmor.

Poster som klassificeras som `calendar` sparas i `calendar_events` i stället för
att kastas. Datumet läses ur rubriken (svenska eller engelska); `next_report`
ger varje bolags närmaste kommande rapportdatum.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone

MONTHS = {
    "januari": 1, "jan": 1, "january": 1,
    "februari": 2, "feb": 2, "february": 2,
    "mars": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4,
    "maj": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "augusti": 8, "aug": 8, "august": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))

ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DAY_MONTH = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?::e|st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}})\b)?", re.I)
MONTH_DAY = re.compile(
    rf"\b({_MONTH})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}})\b)?(?!\d|:)", re.I)

REPORT = re.compile(
    r"delårsrapport|kvartalsrapport|bokslutskommuniké|årsredovisning|halvårsrapport|\bq[1-4]\b|"
    r"interim report|quarterly report|year[- ]end report|half[- ]year report|annual report|"
    r"rapportpresentation|report (?:call|presentation)|presentation of the (?:interim|year|quarterly)|"
    r"earnings (?:call|presentation)|presentation av .{0,20}rapport", re.I)
MEETING = re.compile(r"stämma|general meeting|\bagm\b|\begm\b|shareholders'? meeting", re.I)


def kind_of(title: str) -> str:
    """report, meeting eller other."""
    if REPORT.search(title or ""):
        return "report"
    if MEETING.search(title or ""):
        return "meeting"
    return "other"


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_event_date(title: str, published_at: str | None, *, roll_forward: bool = True) -> str | None:
    """Första datumet i texten som YYYY-MM-DD, annars None.

    Saknas år används publiceringsåret; ligger datumet före publiceringsdagen (och `roll_forward`
    gäller) tas nästa år. Utan publiceringsdatum och utan år returneras None.
    """
    text = title or ""
    published: date | None = None
    if published_at:
        try:
            published = date.fromisoformat(published_at[:10])
        except ValueError:
            published = None
    found: list[tuple[int, date]] = []
    for m in ISO.finditer(text):
        value = _make(int(m[1]), int(m[2]), int(m[3]))
        if value:
            found.append((m.start(), value))
    candidates: list[tuple[int, int, int, str | None]] = []
    for m in DAY_MONTH.finditer(text):
        candidates.append((m.start(), MONTHS[m[2].lower()], int(m[1]), m[3]))
    for m in MONTH_DAY.finditer(text):
        candidates.append((m.start(), MONTHS[m[1].lower()], int(m[2]), m[3]))
    for position, month, day, year in candidates:
        if year:
            value = _make(int(year), month, day)
        elif published is not None:
            value = _make(published.year, month, day)
            if value and roll_forward and value < published:
                value = _make(published.year + 1, month, day)
        else:
            value = None
        if value:
            found.append((position, value))
    if not found:
        return None
    return min(found, key=lambda pair: pair[0])[1].isoformat()


def store_item(connection: sqlite3.Connection, company_id: int, item) -> bool:
    """Spara en kalenderpost (MfnItem). Sant om den var ny."""
    with connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO calendar_events(company_id, source_url, title, published_at, event_date,
               kind, created_at) VALUES (?,?,?,?,?,?,?)""",
            (company_id, item.url, item.title, item.published_at,
             parse_event_date(item.title, item.published_at), kind_of(item.title),
             datetime.now(timezone.utc).replace(microsecond=0).isoformat()))
    return cursor.rowcount > 0


def next_report(connection: sqlite3.Connection, today: str) -> dict[int, dict]:
    """Per bolag den tidigaste rapportposten med datum från och med `today`."""
    result: dict[int, dict] = {}
    rows = connection.execute(
        """SELECT company_id, event_date, title FROM calendar_events
           WHERE kind='report' AND event_date IS NOT NULL AND event_date >= ?
           ORDER BY event_date ASC, id ASC""", (today[:10],))
    for row in rows:
        result.setdefault(row["company_id"], {"date": row["event_date"], "title": row["title"]})
    return result
