"""Kurshistorik i sammandrag per bolag från Nasdaqs publika API (samma API som marketcap.py).

Flöde: orderboks-id via ISIN-sökning (sparas efter första gången), därefter ett anrop mot
`instruments/<id>/chart?fromDate=&toDate=` som ger dagsdata (stängning, högsta, lägsta, volym).
Ur dagsdatan räknas kurs, förändring 1/3/12 månader, snittomsättning och snittvolym över tre
månader samt 52-veckors högsta/lägsta. Saknas underlag lämnas fältet tomt: inget hittas på.

Bolag som inte handlas hos Nasdaq (t.ex. Spotlight och NGM) får ett felmeddelande sparat och
hämtas inte om förrän nästa dag. Varje bolag hämtas högst en gång per dag, med paus emellan.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import marketcap
from .settings import Settings

CHART_URL = marketcap.BASE + "/instruments/{order_book}/chart?assetClass=SHARES&fromDate={start}&toDate={end}&lang=en"
HISTORY_DAYS = 372          # lite mer än ett år så att 12-månadersjämförelsen får en punkt att luta sig mot
MIN_TURNOVER_DAYS = 20      # färre handelsdagar på tre månader ger ingen pålitlig snittomsättning
YEAR_COVERAGE_DAYS = 350    # 52-veckorsvärden kräver historik som täcker i stort sett hela året
DELAY = marketcap.DELAY
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

Fetcher = marketcap.Fetcher


def _number(value: object) -> float | None:
    """Nasdaqs talformat är engelskt: '1,318,179' och '3.664'."""
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[\s ,]", "", str(value or ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_chart(payload: dict) -> list[dict]:
    """Dagspunkter [{date, close, high, low, volume}] i datumordning ur chart-svaret.

    Punkter utan datum (intradag har klockslag) eller utan stängningskurs hoppas över."""
    points = []
    for point in (payload.get("data") or {}).get("CP") or []:
        z = point.get("z") or {}
        day = str(z.get("dateTime") or "")
        close = _number(z.get("close") if z.get("close") is not None else z.get("value"))
        if not DATE.fullmatch(day) or close is None or close <= 0:
            continue
        points.append({"date": day, "close": close, "high": _number(z.get("high")),
                       "low": _number(z.get("low")), "volume": _number(z.get("volume"))})
    points.sort(key=lambda p: p["date"])
    return points


def _months_back(day: date, months: int) -> date:
    year, month = day.year, day.month - months
    while month < 1:
        month += 12
        year -= 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def _close_on_or_before(points: list[dict], target: date) -> float | None:
    best = None
    for point in points:
        if point["date"] <= target.isoformat():
            best = point["close"]
        else:
            break
    return best


def _change_pct(points: list[dict], last: dict, months: int) -> float | None:
    base = _close_on_or_before(points, _months_back(date.fromisoformat(last["date"]), months))
    return round(100 * (last["close"] / base - 1), 2) if base else None


def weekly_series(points: list[dict], as_of: date) -> list[list]:
    """Sista stängningskursen per vecka det senaste året: [[datum, kurs], ...] för kursdiagrammet."""
    cutoff = (as_of - timedelta(days=366)).isoformat()
    weeks: dict[tuple[int, int], list] = {}
    for point in points:
        if point["date"] > cutoff and point["close"] is not None:
            weeks[tuple(date.fromisoformat(point["date"]).isocalendar()[:2])] = [point["date"], point["close"]]
    return list(weeks.values())


def summarize(points: list[dict], sek_per_unit: float | None = 1.0) -> dict:
    """Sammandrag ur dagspunkter. `sek_per_unit` omvandlar omsättning till SEK (None = okänd kurs)."""
    if not points:
        raise marketcap.MarketDataError("kurshistorik saknas")
    last = points[-1]
    as_of = date.fromisoformat(last["date"])
    window = [p for p in points if p["date"] > _months_back(as_of, 3).isoformat()]
    turnover = volume = None
    traded = [p for p in window if p["volume"] is not None]
    if len(traded) >= MIN_TURNOVER_DAYS:
        volume = sum(p["volume"] for p in traded) / len(traded)
        if sek_per_unit:
            turnover = sum(p["volume"] * p["close"] for p in traded) * sek_per_unit / len(traded)
    year = [p for p in points if p["date"] > (as_of - timedelta(days=365)).isoformat()]
    covered = (as_of - date.fromisoformat(points[0]["date"])).days >= YEAR_COVERAGE_DAYS
    highs = [p["high"] if p["high"] is not None else p["close"] for p in year]
    lows = [p["low"] if p["low"] is not None else p["close"] for p in year]
    return {"price": last["close"], "price_change_1m_pct": _change_pct(points, last, 1),
            "price_change_3m_pct": _change_pct(points, last, 3),
            "price_change_12m_pct": _change_pct(points, last, 12),
            "avg_daily_turnover_3m": round(turnover) if turnover is not None else None,
            "avg_daily_volume_3m": round(volume) if volume is not None else None,
            "high_52w": max(highs) if covered and highs else None,
            "low_52w": min(lows) if covered and lows else None, "as_of": last["date"],
            "series": json.dumps(weekly_series(points, as_of))}


_DAY: str | None = None   # dagen som körningen gäller; tidsstämplar får samma dag som kontrollen jämför med


def _now() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return f"{_DAY}{stamp[10:]}" if _DAY else stamp


FIELDS = ("price", "price_change_1m_pct", "price_change_3m_pct", "price_change_12m_pct",
          "avg_daily_turnover_3m", "avg_daily_volume_3m", "high_52w", "low_52w", "as_of", "series")


def store(connection: sqlite3.Connection, company_id: int, isin: str | None, *, order_book: str | None = None,
          currency: str | None = None, summary: dict | None = None, error: str | None = None) -> None:
    summary = summary or {}
    with connection:
        connection.execute(
            f"""INSERT INTO market_history_summary(company_id, isin, order_book, currency, {', '.join(FIELDS)},
                source, fetched_at, error) VALUES (?,?,?,?,{','.join('?' * len(FIELDS))},?,?,?)
                ON CONFLICT(company_id) DO UPDATE SET isin=excluded.isin,
                order_book=COALESCE(excluded.order_book, market_history_summary.order_book),
                currency=excluded.currency, {', '.join(f'{f}=excluded.{f}' for f in FIELDS)},
                source=excluded.source, fetched_at=excluded.fetched_at, error=excluded.error""",
            (company_id, isin, order_book, currency, *(summary.get(f) for f in FIELDS), "nasdaq", _now(), error))


def refresh_company(connection: sqlite3.Connection, company_id: int, isin: str | None, *, today: str,
                    rates: dict[str, float] | None = None, fetch: Fetcher = marketcap.fetch_json,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    """Hämta och spara kurssammandraget för ett bolag. True om det lyckades."""
    if not isin:
        store(connection, company_id, None, error="ISIN saknas")
        return False
    known = connection.execute("SELECT order_book, currency FROM market_history_summary WHERE company_id=? "
                               "AND isin=?", (company_id, isin)).fetchone()
    order_book, currency = (known["order_book"], known["currency"]) if known else (None, None)
    try:
        if not order_book:
            share = marketcap.find_share(fetch(marketcap.SEARCH_URL.format(isin=urllib.parse.quote(isin))))
            if share is None:
                raise marketcap.MarketDataError("hittades inte hos Nasdaq")
            order_book, currency = share["orderbookId"], share.get("currency") or "SEK"
            sleep(DELAY)
        start = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
        points = parse_chart(fetch(CHART_URL.format(order_book=urllib.parse.quote(order_book),
                                                    start=start, end=today)))
        factor = 1.0 if (currency or "SEK").upper() == "SEK" else (rates or {}).get((currency or "").upper())
        summary = summarize(points, factor)
    except marketcap.MarketDataError as error:
        store(connection, company_id, isin, order_book=order_book, currency=currency, error=str(error))
        return False
    store(connection, company_id, isin, order_book=order_book, currency=currency, summary=summary)
    return True


def candidates(connection: sqlite3.Connection, today: str) -> list[tuple[int, str | None]]:
    """Bolag med ISIN och beräknad runway som inte hämtats idag; äldst hämtade (eller aldrig) först."""
    rows = connection.execute(
        """SELECT DISTINCT c.id, c.isin, h.fetched_at FROM companies c
           JOIN reports r ON r.company_id = c.id JOIN runway_results x ON x.document_id = r.document_id
           LEFT JOIN market_history_summary h ON h.company_id = c.id
           WHERE c.isin IS NOT NULL AND c.isin <> ''
           ORDER BY h.fetched_at IS NOT NULL, h.fetched_at, c.id""").fetchall()
    return [(r["id"], r["isin"]) for r in rows if not (r["fetched_at"] or "").startswith(today)]


def run(connection: sqlite3.Connection, settings: Settings, *, limit: int | None = None,
        log: Callable[[str], None] = print, fetch: Fetcher = marketcap.fetch_json,
        sleep: Callable[[float], None] = time.sleep, today: str | None = None) -> dict[str, int]:
    """Hämta kurssammandrag för bolag som inte hämtats idag; högst `limit` bolag per körning."""
    from .fx import latest_rates

    today = today or date.today().isoformat()
    global _DAY
    _DAY = today
    wanted = candidates(connection, today)
    counts = {"fetched": 0, "failed": 0, "remaining": 0}
    rates = latest_rates(connection)
    for index, (company_id, isin) in enumerate(wanted):
        if limit is not None and index >= limit:
            counts["remaining"] = len(wanted) - index
            break
        if index:
            sleep(DELAY)
        ok = refresh_company(connection, company_id, isin, today=today, rates=rates, fetch=fetch, sleep=sleep)
        counts["fetched" if ok else "failed"] += 1
    log(f"kurshistorik: {counts}")
    return counts


def load(connection: sqlite3.Connection, company_id: int) -> dict | None:
    row = connection.execute("SELECT * FROM market_history_summary WHERE company_id=?", (company_id,)).fetchone()
    return dict(row) if row else None
