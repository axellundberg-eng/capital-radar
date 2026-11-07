"""Blankning ur Finansinspektionens blankningsregister (fi.se/sv/vara-register/blankningsregistret/).

Aktuella nettopositioner (>= 0,5 % av aktiekapitalet) laddas ner som ODS-fil:

    https://www.fi.se/sv/vara-register/blankningsregistret/GetAktuellFile/

Filen har ett blad med rubrikrad: innehavare, emittentens namn, ISIN, position i procent (svenskt decimalkomma),
positionsdatum. ODS är en zip med content.xml, så den läses med standardbiblioteket. Rader matchas mot bolag med
ISIN först och därefter normaliserat namn (som insynshandeln). Omatchade rader sparas inte. Varje lyckad hämtning
ersätter hela den tidigare ögonblicksbilden; ett fel lämnar den orörd. Högst en hämtning per dag.
"""

from __future__ import annotations

import io
import re
import sqlite3
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Callable

from .insiders import normalize_name
from .settings import Settings

URL = "https://www.fi.se/sv/vara-register/blankningsregistret/GetAktuellFile/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 30.0
MAX_BYTES = 10_000_000
MAX_COLUMNS = 40   # ODS repeterar tomma celler tusentals gånger; allt efter det är tomt

_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

Fetcher = Callable[[str], bytes]


class ShortError(Exception):
    pass


def fetch_file(url: str = URL, timeout: float = TIMEOUT) -> bytes:
    """Hämta registret. httpx används i stället för urllib: FI:s server omdirigerar till en 404-sida när
    anropet begär okomprimerat innehåll och stänger anslutningen, vilket urllib alltid gör."""
    import httpx

    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=min(timeout, 60.0),
                             follow_redirects=False)
        response.raise_for_status()
        return response.content[:MAX_BYTES]
    except Exception as error:  # nät- och HTTP-fel behandlas lika
        raise ShortError(f"anrop misslyckades: {type(error).__name__}") from error


def read_ods(raw: bytes) -> list[list[str]]:
    """Första bladets rader som textceller (tomma svansceller och tomma rader borttagna)."""
    try:
        content = zipfile.ZipFile(io.BytesIO(raw)).read("content.xml")
        root = ET.fromstring(content)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        raise ShortError("svaret är ingen läsbar ODS-fil") from error
    rows: list[list[str]] = []
    sheet = next(root.iter(_TABLE + "table"), None)
    if sheet is None:
        raise ShortError("ODS-filen saknar blad")
    for row in sheet.iter(_TABLE + "table-row"):
        cells: list[str] = []
        for cell in row:
            if cell.tag not in (_TABLE + "table-cell", _TABLE + "covered-table-cell"):
                continue
            repeat = min(int(cell.get(_TABLE + "number-columns-repeated", "1")), MAX_COLUMNS)
            text = " ".join("".join(p.itertext()) for p in cell.iter(_TEXT + "p")).replace("\xa0", " ").strip()
            cells.extend([text] * repeat)
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells[:MAX_COLUMNS])
    return rows


def _number(text: str | None) -> float | None:
    cleaned = re.sub(r"[\s\xa0]", "", text or "").replace(",", ".").replace("%", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_positions(rows: list[list[str]]) -> list[dict]:
    """Positioner {holder, issuer, isin, pct, date} ur radlistan; rubrikraden hittas på 'ISIN'."""
    header_index = next((i for i, r in enumerate(rows) if any(c.strip().upper() == "ISIN" for c in r)), None)
    if header_index is None:
        raise ShortError("rubrikrad med ISIN saknas (ändrat format?)")
    header = [c.casefold() for c in rows[header_index]]

    def column(*needles: str) -> int | None:
        return next((i for i, c in enumerate(header) if any(n in c for n in needles)), None)

    holder_i, issuer_i = column("innehavare", "position holder"), column("emittent", "issuer")
    isin_i, pct_i, date_i = column("isin"), column("position i procent", "per cent"), column("positionen (position date)", "datum")
    if None in (holder_i, issuer_i, isin_i, pct_i):
        raise ShortError("kolumner saknas i blankningsfilen")

    def cell(row: list[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    positions = []
    for row in rows[header_index + 1:]:
        pct = _number(cell(row, pct_i))
        holder = cell(row, holder_i)
        if pct is None or not holder:
            continue
        positions.append({"holder": holder, "issuer": cell(row, issuer_i), "isin": cell(row, isin_i).upper() or None,
                          "pct": pct, "date": cell(row, date_i)[:10] or None})
    return positions


def match_positions(connection: sqlite3.Connection, positions: list[dict]) -> list[dict]:
    """Positioner med company_id: ISIN först, därefter identiskt normaliserat namn."""
    by_isin: dict[str, int] = {}
    by_name: dict[str, set[int]] = {}
    for company in connection.execute("SELECT id, name, isin FROM companies"):
        if company["isin"]:
            by_isin[company["isin"].upper()] = company["id"]
        by_name.setdefault(normalize_name(company["name"]), set()).add(company["id"])
    matched = []
    for position in positions:
        company_id = by_isin.get(position["isin"] or "")
        if company_id is None:
            ids = by_name.get(normalize_name(position["issuer"]), set())
            company_id = next(iter(ids)) if len(ids) == 1 else None   # tvetydigt namn matchas inte
        if company_id is not None:
            matched.append({**position, "company_id": company_id})
    return matched


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def store(connection: sqlite3.Connection, matched: list[dict], rows: int) -> None:
    """Ersätt ögonblicksbilden och notera hämtningen i en transaktion."""
    now = _now()
    with connection:
        connection.execute("DELETE FROM short_positions")
        connection.executemany(
            "INSERT INTO short_positions(company_id, holder, pct, position_date, fetched_at) VALUES (?,?,?,?,?)",
            [(m["company_id"], m["holder"], m["pct"], m["date"], now) for m in matched])
        connection.execute(
            """INSERT INTO short_fetch(id, fetched_at, rows, matched, error) VALUES (1,?,?,?,NULL)
               ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at, rows=excluded.rows,
               matched=excluded.matched, error=NULL""", (now, rows, len(matched)))


def _record_error(connection: sqlite3.Connection, error: str) -> None:
    with connection:
        connection.execute(
            """INSERT INTO short_fetch(id, fetched_at, rows, matched, error) VALUES (1,?,0,0,?)
               ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at, error=excluded.error""",
            (_now(), error))


def run(connection: sqlite3.Connection, settings: Settings | None = None, *, log: Callable[[str], None] = print,
        fetch: Fetcher = fetch_file, today: str | None = None) -> dict:
    """Hämta och lagra blankningsregistret, högst en gång per dag (ett misslyckat försök räknas inte)."""
    today = today or date.today().isoformat()
    last = connection.execute("SELECT fetched_at, error FROM short_fetch WHERE id=1").fetchone()
    if last and last["fetched_at"].startswith(today) and not last["error"]:
        return {"skipped": 1, "fetched": 0, "rows": 0, "matched": 0}
    try:
        positions = parse_positions(read_ods(fetch(URL)))
    except ShortError as error:
        _record_error(connection, str(error))
        log(f"blankning misslyckades: {error}")
        return {"skipped": 0, "fetched": 0, "failed": 1, "rows": 0, "matched": 0}
    matched = match_positions(connection, positions)
    store(connection, matched, len(positions))
    counts = {"skipped": 0, "fetched": 1, "rows": len(positions), "matched": len(matched)}
    log(f"blankning: {counts}")
    return counts


def for_company(connection: sqlite3.Connection, company_id: int) -> list[dict]:
    """Aktuella positioner för bolaget, störst först."""
    return [{"holder": r["holder"], "pct": r["pct"], "date": r["position_date"]}
            for r in connection.execute(
                "SELECT * FROM short_positions WHERE company_id=? ORDER BY pct DESC, holder", (company_id,))]
