"""Lägg till ett bolag som saknas i universumet (som bara byggs från den gamla databasen).

    python -m radar add --slug foremont-group --name "Foremont Group" [--isin SE... --symbol FORE --market Spotlight]

Bolaget får ett eget id från 100 000 (kolliderar aldrig med den gamla databasens id). Därefter hämtas dess
MFN-sida (rapporter, nyheter, kalender), den senaste rapporten läses och räknas, finansieringsnyheterna de
senaste 24 månaderna granskas och branschen klassas. Ett bolag som redan finns (samma slug) läggs inte till igen.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Callable

from . import mfn
from .collect import collect_company
from .settings import Settings

FIRST_ID = 100_000
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,80}")
MARKETS = ("Spotlight", "First North", "Nasdaq Stockholm", "NGM", "Main Market")


def next_id(connection: sqlite3.Connection) -> int:
    return max(FIRST_ID, (connection.execute("SELECT MAX(id) FROM companies").fetchone()[0] or 0) + 1)


def add(connection: sqlite3.Connection, slug: str, name: str, *, isin: str | None = None, symbol: str | None = None,
        market: str | None = None, segment: str | None = None) -> tuple[int, bool]:
    """(id, ny). Samma slug eller ISIN ger det befintliga bolaget tillbaka i stället för en dubblett."""
    slug = slug.strip().lower()
    if not SLUG.fullmatch(slug):
        raise ValueError(f"ogiltig MFN-slug: {slug!r}")
    row = connection.execute("SELECT id FROM companies WHERE mfn_slug=? OR (isin IS NOT NULL AND isin=?)",
                             (slug, isin)).fetchone()
    if row:
        return row[0], False
    company_id = next_id(connection)
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin, symbol, market, segment, mfn_slug) VALUES (?,?,?,?,?,?,?)",
                           (company_id, name.strip(), isin, symbol, market, segment, slug))
    return company_id, True


def detect_market(html: str) -> str | None:
    """Handelsplats ur MFN-sidans text, om den nämns."""
    for market in MARKETS:
        if re.search(re.escape(market), html, re.I):
            return market
    return None


def run(connection: sqlite3.Connection, settings: Settings, client, slug: str, name: str, *, today: str | None = None,
        isin: str | None = None, symbol: str | None = None, market: str | None = None,
        log: Callable[[str], None] = print) -> dict:
    """Lägg till bolaget och fyll på det. `client` kan vara None: då hoppas AI-stegen över."""
    from .batch import process_with_fallback
    from .news import process_company_news

    today = today or date.today().isoformat()
    html = mfn.fetch_html(mfn.page_url(slug), settings, None)       # misslyckas tydligt om sluggen är fel
    company_id, created = add(connection, slug, name, isin=isin, symbol=symbol, market=market or detect_market(html))
    result: dict = {"company_id": company_id, "created": created}
    result["collect"] = collect_company(connection, settings, {"id": company_id, "name": name, "mfn_slug": slug})
    document = connection.execute(
        "SELECT document_id FROM reports WHERE company_id=? AND pdf_url IS NOT NULL ORDER BY published_at DESC LIMIT 1",
        (company_id,)).fetchone()
    if client is None:
        result["ai"] = "hoppades över: ingen modellnyckel"
        return result
    if document:
        outcome = process_with_fallback(connection, settings, client, document[0], today=today)
        result["runway"] = (outcome.get("runway") or {}).get("runway_today")
    cutoff = date(int(today[:4]) - 2, int(today[5:7]), min(int(today[8:10]), 28)).isoformat()
    result["news"] = process_company_news(connection, settings, client, company_id, cutoff, max_items=60)
    from . import sectors

    result["sector"] = sectors.classify(connection, client, limit=1)
    return result
