"""Växelkurser från Riksbanken (SEK per enhet av valutan), sparade i tabellen fx_rates.

Alla belopp i prospektpoängen räknas i SEK, så att kapitalbehov, storlek och andel av marknadsvärdet
jämförs i samma valuta. Runway-motorn räknar i rapportens valuta och använder kurserna för att räkna
om finansieringshändelser i annan valuta. Kurser gissas aldrig: saknas en kurs blir omräkningen None
och beloppet räknas inte (det flaggas i stället).

Riksbankens API har en hastighetsgräns på några anrop per minut, så kurserna hämtas högst en gång per
dag och valuta, med en paus mellan anropen.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

API = "https://api.riksbank.se/swea/v1/Observations/Latest/{series}"
# Serie per valuta; alla anges som kronor per en enhet av valutan.
SERIES = {"EUR": "SEKEURPMI", "USD": "SEKUSDPMI", "DKK": "SEKDKKPMI", "NOK": "SEKNOKPMI",
          "GBP": "SEKGBPPMI", "CAD": "SEKCADPMI", "CHF": "SEKCHFPMI", "CNY": "SEKCNYPMI"}
DELAY_SECONDS = 15.0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fetch(series: str, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(API.format(series=series), headers={"Accept": "application/json",
                                                                        "User-Agent": "CapitalRadar/0.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fast adress
        return json.loads(response.read().decode("utf-8"))


def store(connection: sqlite3.Connection, currency: str, rate_date: str, sek_per_unit: float,
          source: str = "riksbank") -> None:
    with connection:
        connection.execute(
            """INSERT INTO fx_rates(currency, rate_date, sek_per_unit, source, fetched_at) VALUES (?,?,?,?,?)
               ON CONFLICT(currency, rate_date) DO UPDATE SET sek_per_unit=excluded.sek_per_unit,
               fetched_at=excluded.fetched_at""", (currency, rate_date, sek_per_unit, source, _now()))


def needed_currencies(connection: sqlite3.Connection) -> set[str]:
    """Valutor som förekommer i rapporter, händelser och marknadsvärden (utom SEK)."""
    rows = connection.execute(
        """SELECT currency FROM facts UNION SELECT currency FROM financing_events
           UNION SELECT currency FROM market_data""").fetchall()
    return {r[0].upper() for r in rows if r[0] and r[0].upper() != "SEK"}


def refresh(connection: sqlite3.Connection, currencies: set[str] | None = None, *,
            fetch: Callable[[str], dict] = _fetch, sleep: Callable[[float], None] = time.sleep,
            today: str | None = None) -> dict[str, int]:
    """Hämta dagens kurs för valutor som saknar en kurs hämtad idag."""
    today = today or date.today().isoformat()
    currencies = needed_currencies(connection) if currencies is None else currencies
    counts = {"fetched": 0, "fresh": 0, "unsupported": 0, "failed": 0}
    first = True
    for currency in sorted(currencies):
        series = SERIES.get(currency)
        if series is None:
            counts["unsupported"] += 1
            continue
        latest = connection.execute("SELECT MAX(fetched_at) FROM fx_rates WHERE currency=?", (currency,)).fetchone()[0]
        if latest and latest[:10] == today:
            counts["fresh"] += 1
            continue
        if not first:
            sleep(DELAY_SECONDS)
        first = False
        try:
            data = fetch(series)
            store(connection, currency, str(data["date"])[:10], float(data["value"]))
            counts["fetched"] += 1
        except Exception as error:  # noqa: BLE001 - en saknad kurs ska aldrig stoppa körningen
            logger.warning("växelkurs för %s kunde inte hämtas: %s", currency, error)
            counts["failed"] += 1
    return counts


def latest_rates(connection: sqlite3.Connection) -> dict[str, float]:
    """Senaste kända kurs per valuta (SEK per enhet). SEK ingår alltid med 1."""
    rates = {"SEK": 1.0}
    for currency, value in connection.execute(
            """SELECT f.currency, f.sek_per_unit FROM fx_rates f
               JOIN (SELECT currency, MAX(rate_date) d FROM fx_rates GROUP BY currency) l
               ON l.currency = f.currency AND l.d = f.rate_date"""):
        rates[currency] = value
    return rates


def rate_on(connection: sqlite3.Connection, currency: str, day: str | None = None) -> float | None:
    """Kurs på eller närmast före `day`, annars den senaste kända. None om valutan saknas helt."""
    currency = (currency or "SEK").upper()
    if currency == "SEK":
        return 1.0
    row = None
    if day:
        row = connection.execute("""SELECT sek_per_unit FROM fx_rates WHERE currency=? AND rate_date <= ?
                                    ORDER BY rate_date DESC LIMIT 1""", (currency, day[:10])).fetchone()
    if row is None:
        row = connection.execute("SELECT sek_per_unit FROM fx_rates WHERE currency=? ORDER BY rate_date DESC LIMIT 1",
                                 (currency,)).fetchone()
    return None if row is None else row[0]


def convert(amount: float | None, from_currency: str | None, to_currency: str | None,
            rates: dict[str, float]) -> float | None:
    """Räkna om med en kurstabell (SEK per enhet). None om en kurs saknas."""
    if amount is None:
        return None
    src, dst = (from_currency or "SEK").upper(), (to_currency or "SEK").upper()
    if src == dst:
        return amount
    if src not in rates or dst not in rates:
        return None
    return amount * rates[src] / rates[dst]


def converter(connection: sqlite3.Connection) -> Callable[[float, str, str, str | None], float | None]:
    """Omräkning med kursen på händelsens datum (eller senaste kända), för runway-motorn."""
    def run(amount: float, from_currency: str, to_currency: str, day: str | None = None) -> float | None:
        src, dst = rate_on(connection, from_currency, day), rate_on(connection, to_currency, day)
        if amount is None or src is None or dst is None:
            return None
        return amount * src / dst
    return run
