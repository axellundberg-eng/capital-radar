"""Marknadsvärde per bolag från Nasdaqs publika API (via ISIN), med cache.

Flöde: sök på ISIN, välj aktieinstrumentet (assetClass SHARES) och läs
`marketCap` ur instrumentets summary. Svaret sparas i `market_data`; fel sparas
också (kolumnen `error`) så att bolag som saknas hos Nasdaq (t.ex. NGM) visas
som "okänt" och inte hämtas om i onödan. Hämtningen går sakta: minst 0,5 s
mellan anrop, högst 15 s per anrop. Data yngre än 7 dagar hämtas inte om.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

BASE = "https://api.nasdaq.com/api/nordic"
SEARCH_URL = BASE + "/search?searchText={isin}"
SUMMARY_URL = BASE + "/instruments/{order_book}/summary?assetClass=SHARES"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 15.0
DELAY = 0.6
MAX_AGE_DAYS = 7

Fetcher = Callable[[str], dict]


class MarketDataError(Exception):
    pass


def fetch_json(url: str, timeout: float = TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, TIMEOUT)) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:  # nät-, HTTP- och JSON-fel behandlas lika
        raise MarketDataError(f"anrop misslyckades: {type(error).__name__}") from error


def parse_number(text: object) -> float | None:
    """'1 234 567 890' och '1,234,567,890' (även decimaler som '12,5') till tal."""
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[\s\u00a0]", "", str(text or ""))
    if not re.fullmatch(r"\d[\d.,]*", cleaned):
        return None
    last = max(cleaned.rfind("."), cleaned.rfind(","))
    if last < 0:
        return float(cleaned)
    head, tail, sep = cleaned[:last], cleaned[last + 1:], cleaned[last]
    decimal = sep not in head and (len(tail) != 3 or "." in head or "," in head)
    digits = re.sub(r"[.,]", "", head)
    return float(f"{digits}.{tail}") if decimal else float(digits + tail)


def find_share(search: dict) -> dict | None:
    """Första aktieinstrumentet (SHARES) i sökresultatet."""
    for group in search.get("data") or []:
        for instrument in group.get("instruments") or []:
            if instrument.get("assetClass") == "SHARES" and instrument.get("orderbookId"):
                return instrument
    return None


def parse_summary(summary: dict, default_currency: str = "SEK") -> tuple[float, str, str | None]:
    """(marknadsvärde, valuta, sektor) ur summary-svaret."""
    data = (summary.get("data") or {}).get("summaryData")
    if not isinstance(data, dict):
        raise MarketDataError("summary saknar summaryData")
    cap = parse_number((data.get("marketCap") or {}).get("value"))
    if not cap or cap <= 0:
        raise MarketDataError("marknadsvärde saknas")
    sector = (data.get("sectorName") or {}).get("value") or None
    return cap, default_currency, sector


def lookup(isin: str, fetch: Fetcher = fetch_json, sleep: Callable[[float], None] = time.sleep
           ) -> tuple[float, str, str | None]:
    """Hämta marknadsvärde för ett ISIN: två anrop med paus emellan."""
    share = find_share(fetch(SEARCH_URL.format(isin=urllib.parse.quote(isin))))
    if share is None:
        raise MarketDataError("hittades inte hos Nasdaq")
    sleep(DELAY)
    return parse_summary(fetch(SUMMARY_URL.format(order_book=urllib.parse.quote(share["orderbookId"]))),
                         share.get("currency") or "SEK")


def is_fresh(fetched_at: str | None, today: str) -> bool:
    if not fetched_at:
        return False
    return datetime.fromisoformat(fetched_at[:10]) + timedelta(days=MAX_AGE_DAYS) > datetime.fromisoformat(today[:10])


def store(connection: sqlite3.Connection, company_id: int, isin: str | None, *, cap: float | None = None,
          currency: str | None = None, sector: str | None = None, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connection:
        connection.execute(
            """INSERT INTO market_data(company_id, isin, market_cap, currency, source, fetched_at, error, sector)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET isin=excluded.isin,
               market_cap=excluded.market_cap, currency=excluded.currency, source=excluded.source,
               fetched_at=excluded.fetched_at, error=excluded.error, sector=excluded.sector""",
            (company_id, isin, cap, currency, "nasdaq", now, error, sector))


def refresh_company(connection: sqlite3.Connection, company_id: int, isin: str | None,
                    fetch: Fetcher = fetch_json, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Hämta och spara marknadsvärde. Returnerar True om värdet hittades."""
    if not isin:
        store(connection, company_id, None, error="ISIN saknas")
        return False
    try:
        cap, currency, sector = lookup(isin, fetch, sleep)
    except MarketDataError as error:
        store(connection, company_id, isin, error=str(error))
        return False
    store(connection, company_id, isin, cap=cap, currency=currency, sector=sector)
    return True


def refresh_many(connection: sqlite3.Connection, companies: list[tuple[int, str | None]], today: str, *,
                 force: bool = False, fetch: Fetcher = fetch_json,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """Hämta saknade eller gamla värden (äldre än 7 dagar); paus mellan bolag."""
    counts = {"fetched": 0, "failed": 0, "cached": 0}
    first = True
    for company_id, isin in companies:
        row = connection.execute("SELECT fetched_at, isin FROM market_data WHERE company_id=?", (company_id,)).fetchone()
        if row and not force and row["isin"] == isin and is_fresh(row["fetched_at"], today):
            counts["cached"] += 1
            continue
        if not first:
            sleep(DELAY)
        first = False
        counts["fetched" if refresh_company(connection, company_id, isin, fetch, sleep) else "failed"] += 1
    return counts
