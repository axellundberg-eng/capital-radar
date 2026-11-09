"""Insynshandel ur Finansinspektionens insynsregister (marknadssok.fi.se, Publiceringsklient).

Exporten är en GET mot sökformuläret med `button=export`:

    https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search
        ?SearchFunctionType=Insyn&Utgivare=<namn>&Transaktionsdatum.From=<ååå-mm-dd>&Transaktionsdatum.To=<...>&button=export

Svaret är semikolonseparerad CSV, i praktiken UTF-16LE utan BOM (äldre/andra varianter som UTF-8 och latin-1
hanteras också). Exporten stängs av när sökningen ger fler än 1000 träffar; då kommer HTML tillbaka och bolaget
får ett fel sparat. Ett anrop per bolag, paus emellan, högst en hämtning per bolag och dag, bara de senaste
12 månaderna.

Bolagsmatchning är försiktig: en rad hör till bolaget bara om instrumentets ISIN är bolagets eller om det
normaliserade utgivarnamnet är identiskt. Övriga rader räknas som omatchade och sparas inte.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .settings import Settings

EXPORT_URL = ("https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search?SearchFunctionType=Insyn"
              "&Utgivare={issuer}&Transaktionsdatum.From={start}&Transaktionsdatum.To={end}&button=export")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 30.0
DELAY = 1.0
MONTHS_BACK = 12
MAX_BYTES = 5_000_000

Fetcher = Callable[[str], bytes]


class InsiderError(Exception):
    pass


def fetch_export(url: str, timeout: float = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 60.0)) as response:
            return response.read(MAX_BYTES)
    except Exception as error:  # nät- och HTTP-fel behandlas lika
        raise InsiderError(f"anrop misslyckades: {type(error).__name__}") from error


def decode_export(raw: bytes) -> str:
    """CSV-text ur exportens byte: UTF-16 (med eller utan BOM), annars UTF-8 och till sist latin-1."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) >= 2 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    if len(raw) >= 2 and raw[0:1] == b"\x00":
        return raw.decode("utf-16-be")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _clean(text: str | None) -> str | None:
    value = (text or "").replace(" ", " ").strip()
    return value or None


def parse_number(text: str | None) -> float | None:
    """'911950,0', '16 244' och '22,64' till tal."""
    cleaned = re.sub(r"[\s ]", "", text or "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_csv(text: str) -> list[dict]:
    """Rader som ordböcker med exportens svenska kolumnrubriker; tomt om texten inte är en insynsexport."""
    if not text.lstrip("﻿").startswith("Publiceringsdatum"):
        raise InsiderError("svaret är ingen CSV-export (för många träffar eller ändrat format)")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    return [{(k or "").strip(): v for k, v in row.items()} for row in reader if any(row.values())]


def normalize_name(name: str | None) -> str:
    """Utgivarnamn för jämförelse: gemener, utan '(publ)', 'AB', 'aktiebolag' och skiljetecken."""
    text = (name or "").casefold()
    text = re.sub(r"\(\s*publ\s*\)|\bpubl\b", " ", text)
    text = re.sub(r"\baktiebolag(?:et)?\b|\bab\b", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search_term(name: str) -> str:
    """Det som skickas som Utgivare: namnet utan '(publ)' och 'AB'."""
    return normalize_name(name) and re.sub(r"\s+", " ", re.sub(r"(?i)\(\s*publ\s*\)|\baktiebolag(?:et)?\b|\bAB\b", " ", name)).strip()


def belongs_to(row: dict, company_name: str, isin: str | None) -> bool:
    if isin and (_clean(row.get("ISIN")) or "").upper() == isin.upper():
        return True
    wanted = normalize_name(company_name)
    return bool(wanted) and normalize_name(row.get("Emittent")) == wanted


def _date(text: str | None) -> str | None:
    value = _clean(text)
    return value[:10] if value else None


def to_trade(row: dict, company_id: int) -> dict | None:
    """Rad till lagringsform, eller None för strukna/ogiltiga rader (bara status 'Aktuell' sparas)."""
    status = (_clean(row.get("Status")) or "Aktuell").casefold()
    if status != "aktuell":
        return None
    published, transaction_date = _clean(row.get("Publiceringsdatum")), _date(row.get("Transaktionsdatum"))
    person, kind = _clean(row.get("Person i ledande ställning")), _clean(row.get("Karaktär"))
    if not (published and transaction_date and person and kind):
        return None
    volume, price = parse_number(row.get("Volym")), parse_number(row.get("Pris"))
    unit = (_clean(row.get("Volymsenhet")) or "Antal").casefold()
    value = round(volume * price, 2) if volume is not None and price and unit == "antal" else None
    key = "|".join([str(company_id), published, person, kind, transaction_date, str(volume), str(price),
                    _clean(row.get("ISIN")) or "", _clean(row.get("Anmälningsskyldig")) or ""])
    return {"company_id": company_id, "published": published, "transaction_date": transaction_date,
            "person": person, "role": _clean(row.get("Befattning")), "transaction_type": kind,
            "volume": volume, "price": price, "currency": _clean(row.get("Valuta")), "value": value,
            "instrument": _clean(row.get("Instrumentnamn")),
            "source_key": hashlib.sha1(key.encode("utf-8")).hexdigest()}


def store_trades(connection: sqlite3.Connection, trades: list[dict]) -> int:
    """Spara nya handlar (befintliga source_key hoppas över). Returnerar antal nya."""
    new = 0
    with connection:
        for t in trades:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO insider_trades(company_id, published, transaction_date, person, role,
                   transaction_type, volume, price, currency, value, instrument, source_key)
                   VALUES (:company_id,:published,:transaction_date,:person,:role,:transaction_type,:volume,
                   :price,:currency,:value,:instrument,:source_key)""", t)
            new += cursor.rowcount
    return new


def ingest(connection: sqlite3.Connection, company_id: int, company_name: str, isin: str | None,
           rows: list[dict]) -> dict[str, int]:
    """Matcha rader mot bolaget och spara de som hör dit. Returnerar räknare."""
    matched = [r for r in rows if belongs_to(r, company_name, isin)]
    trades = [t for t in (to_trade(r, company_id) for r in matched) if t]
    return {"rows": len(rows), "matched": len(matched), "unmatched": len(rows) - len(matched),
            "new": store_trades(connection, trades)}


_DAY: str | None = None   # dagen som körningen gäller; tidsstämplar får samma dag som kontrollen jämför med


def _now() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return f"{_DAY}{stamp[10:]}" if _DAY else stamp


def _record_fetch(connection: sqlite3.Connection, company_id: int, counts: dict | None, error: str | None) -> None:
    counts = counts or {}
    with connection:
        connection.execute(
            """INSERT INTO insider_fetch(company_id, fetched_at, rows, matched, unmatched, error)
               VALUES (?,?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET fetched_at=excluded.fetched_at,
               rows=excluded.rows, matched=excluded.matched, unmatched=excluded.unmatched, error=excluded.error""",
            (company_id, _now(), counts.get("rows", 0), counts.get("matched", 0), counts.get("unmatched", 0), error))


def refresh_company(connection: sqlite3.Connection, company_id: int, name: str, isin: str | None, *, today: str,
                    fetch: Fetcher = fetch_export) -> dict[str, int] | None:
    """Hämta de senaste 12 månaderna för ett bolag. None vid fel (felet sparas)."""
    start = (date.fromisoformat(today) - timedelta(days=round(365.25 * MONTHS_BACK / 12))).isoformat()
    url = EXPORT_URL.format(issuer=urllib.parse.quote(search_term(name)), start=start, end=today)
    try:
        rows = parse_csv(decode_export(fetch(url)))
    except InsiderError as error:
        _record_fetch(connection, company_id, None, str(error))
        return None
    counts = ingest(connection, company_id, name, isin, rows)
    _record_fetch(connection, company_id, counts, None)
    return counts


def candidates(connection: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """Bolag med beräknad runway som inte hämtats idag; äldst hämtade (eller aldrig) först."""
    rows = connection.execute(
        """SELECT DISTINCT c.id, c.name, c.isin, f.fetched_at FROM companies c
           JOIN reports r ON r.company_id = c.id JOIN runway_results x ON x.document_id = r.document_id
           LEFT JOIN insider_fetch f ON f.company_id = c.id
           ORDER BY f.fetched_at IS NOT NULL, f.fetched_at, c.id""").fetchall()
    return [r for r in rows if not (r["fetched_at"] or "").startswith(today)]


def run(connection: sqlite3.Connection, settings: Settings, *, limit: int | None = None,
        log: Callable[[str], None] = print, fetch: Fetcher = fetch_export,
        sleep: Callable[[float], None] = time.sleep, today: str | None = None) -> dict[str, int]:
    """Hämta insynshandel för bolag som inte hämtats idag (högst `limit` per körning), med paus emellan."""
    today = today or date.today().isoformat()
    global _DAY
    _DAY = today
    wanted = candidates(connection, today)
    counts = {"fetched": 0, "failed": 0, "new_trades": 0, "unmatched_rows": 0, "remaining": 0}
    for index, company in enumerate(wanted):
        if limit is not None and index >= limit:
            counts["remaining"] = len(wanted) - index
            break
        if index:
            sleep(DELAY)
        result = refresh_company(connection, company["id"], company["name"], company["isin"], today=today, fetch=fetch)
        if result is None:
            counts["failed"] += 1
            continue
        counts["fetched"] += 1
        counts["new_trades"] += result["new"]
        counts["unmatched_rows"] += result["unmatched"]
    log(f"insynshandel: {counts}")
    return counts


BUY_TYPES = ("förvärv",)
SELL_TYPES = ("avyttring",)


def summary(connection: sqlite3.Connection, company_id: int, today: str, rates: dict[str, float] | None = None,
            months: int = 6) -> dict:
    """Köp och sälj i SEK de senaste `months` månaderna. Handlar i valuta utan känd kurs räknas inte med."""
    cutoff = (date.fromisoformat(today) - timedelta(days=round(365.25 * months / 12))).isoformat()
    buys = sells = 0.0
    buyers: set[str] = set()
    sellers: set[str] = set()
    for row in connection.execute(
            """SELECT person, transaction_type, value, currency FROM insider_trades
               WHERE company_id=? AND transaction_date >= ? AND value IS NOT NULL""", (company_id, cutoff)):
        factor = 1.0 if (row["currency"] or "SEK").upper() == "SEK" else (rates or {}).get((row["currency"] or "").upper())
        if factor is None:
            continue
        kind = (row["transaction_type"] or "").casefold()
        if kind in BUY_TYPES:
            buys += row["value"] * factor
            buyers.add(row["person"])
        elif kind in SELL_TYPES:
            sells += row["value"] * factor
            sellers.add(row["person"])
    return {"months": months, "buys": round(buys), "sells": round(sells), "net": round(buys - sells),
            "buyers": len(buyers), "sellers": len(sellers)}


def recent(connection: sqlite3.Connection, company_id: int, limit: int = 50) -> list[dict]:
    """Senaste handlarna, nyast först (transaktionsdatum, därefter publicering)."""
    return [{"date": r["transaction_date"], "person": r["person"], "role": r["role"],
             "transaction": r["transaction_type"], "volume": r["volume"], "price": r["price"],
             "value": r["value"], "currency": r["currency"]}
            for r in connection.execute(
                """SELECT * FROM insider_trades WHERE company_id=?
                   ORDER BY transaction_date DESC, published DESC, id DESC LIMIT ?""", (company_id, limit))]
