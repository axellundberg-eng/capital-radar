"""Gruppens historik: en rad per bolag och dag med runway-grupp och runway (band_history).

Skrivs av schemaläggarens varv (högst en rad per bolag och dag, senaste vinner) och ger flödesvyn:
hur många bolag som ligger i varje grupp vecka för vecka och vilka som flyttat sig.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

BANDS = ("acute", "exhausted", "under_3", "3_9", "9_18", "over_18", "raising", "structural", "positive", "missing")
SCHEMA = """
CREATE TABLE IF NOT EXISTS band_history (
    company_id INTEGER NOT NULL, day TEXT NOT NULL, band TEXT NOT NULL, runway_today REAL,
    PRIMARY KEY (company_id, day)
);
CREATE INDEX IF NOT EXISTS band_history_day ON band_history(day);
"""


def snapshot(connection: sqlite3.Connection, items: list[dict], day: str) -> int:
    """Spara dagens grupp per bolag. Samma dag skrivs över, så ett varv i timmen ger en rad per dag."""
    connection.executescript(SCHEMA)
    with connection:
        connection.executemany(
            """INSERT INTO band_history(company_id, day, band, runway_today) VALUES (?,?,?,?)
               ON CONFLICT(company_id, day) DO UPDATE SET band=excluded.band, runway_today=excluded.runway_today""",
            [(item["id"], day[:10], item["band"], item.get("runway_today")) for item in items])
    return len(items)


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def flow(connection: sqlite3.Connection, today: str, weeks: int = 12) -> dict:
    """Per vecka: antal bolag per grupp (sista ögonblicksbilden i veckan) och flyttarna mot veckan innan.

    Veckor utan ögonblicksbild hoppas över, så vyn visar bara data som finns."""
    connection.executescript(SCHEMA)
    end = date.fromisoformat(today[:10])
    start = _week_start(end) - timedelta(weeks=weeks - 1)
    rows = connection.execute(
        """SELECT h.company_id, h.day, h.band, h.runway_today, c.name FROM band_history h
           JOIN companies c ON c.id = h.company_id WHERE h.day >= ? ORDER BY h.day""",
        (start.isoformat(),)).fetchall()
    by_week: dict[str, dict[int, tuple[str, str]]] = {}
    names: dict[int, str] = {}
    for row in rows:
        week = _week_start(date.fromisoformat(row["day"])).isoformat()
        by_week.setdefault(week, {})[row["company_id"]] = (row["band"], row["day"])
        names[row["company_id"]] = row["name"]
    out = []
    previous: dict[int, tuple[str, str]] | None = None
    for week in sorted(by_week):
        current = by_week[week]
        counts = {band: 0 for band in BANDS}
        for band, _day in current.values():
            counts[band] = counts.get(band, 0) + 1
        moves = []
        if previous is not None:
            for company_id, (band, day) in current.items():
                before = previous.get(company_id)
                if before and before[0] != band:
                    moves.append({"id": company_id, "name": names[company_id], "from": before[0], "to": band, "day": day})
        moves.sort(key=lambda m: (BANDS.index(m["to"]) if m["to"] in BANDS else 99, m["name"].casefold()))
        out.append({"week": week, "counts": counts, "companies": len(current), "moves": moves})
        previous = current
    return {"weeks": out, "bands": list(BANDS)}
