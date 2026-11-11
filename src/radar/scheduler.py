"""Steg 6: löpande drift utan manuella körningar.

Varje varv (standard en gång i timmen):
1. Samla nya dokument: RADAR_COLLECT_SOURCE=mfn (standard) hämtar och klassificerar bolagens MFN-sidor själv (collect);
   legacy importerar ur arkivdatabasen (bara tester och engångsimport).
2. Extrahera nya rapporter (senaste per bolag; redan extraherade hoppas över).
3. Läs nya pressmeddelanden för bolag med kort runway.
4. Hämta marknadsvärde för prospekt som saknar färskt värde.
5. Spara läget per bolag och markera förändringar ("Ny i 3–9 mån", "Emission annonserad").

Ett lås hindrar två samtidiga varv. Varje varv har egna tokenbudgetar.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import authorizations, plans
from .batch import run_latest
from .collect import collect_all
from .db import connect
from .extract import ChatClient
from .importer import import_legacy
from .marketcap import refresh_many
from .news import run_news
from .qualify import prospect_companies
from .settings import Settings

logger = logging.getLogger(__name__)
STALE_LOCK_SECONDS = 300       # utan hjärtslag på fem minuter är låset övergivet
HEARTBEAT_SECONDS = 30
RETRY_AFTER_SKIP_SECONDS = 300
OWNERS_TOKEN_BUDGET = 60000    # tokens för ägartolkning per varv
CHANGE_TEXT = {"3_9": "Ny i 3–9 mån", "under_3": "Ny under 3 mån", "exhausted": "Kassan borde vara slut",
               "acute": "Akut: under 1 mån", "structural": "Strukturell förändring"}

STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_status (
    company_id INTEGER PRIMARY KEY,
    band TEXT, raise_announced INTEGER NOT NULL DEFAULT 0, runway_today REAL,
    change TEXT, changed_at TEXT, updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CycleLock:
    """Lås i en fil bredvid databasen, med hjärtslag.

    Medan ett varv pågår uppdateras filens ändringstid var 30:e sekund. Ett lås som inte
    rörts på fem minuter hör till en process som dött (t.ex. en container som ersattes
    mitt i ett varv vid en utrullning) och tas över.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "CycleLock":
        if self.path.exists() and time.time() - self.path.stat().st_mtime > STALE_LOCK_SECONDS:
            self.path.unlink(missing_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, str(os.getpid()).encode())
        self._thread = threading.Thread(target=self._beat, name="radar-lock-heartbeat", daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                os.utime(self.path)
            except OSError:
                return

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def change_for(previous: sqlite3.Row | None, item: dict) -> str | None:
    """Text för en förändring som är värd att visa, annars None."""
    raise_now = "raise_announced" in item["flags"]
    if previous is None:
        return None  # första gången: ingen förändring att jämföra mot
    if raise_now and not previous["raise_announced"]:
        return "Emission annonserad"
    if item["band"] != previous["band"] and item["band"] in CHANGE_TEXT:
        return CHANGE_TEXT[item["band"]]
    return None


def record_changes(connection: sqlite3.Connection, items: list[dict]) -> int:
    connection.executescript(STATUS_SCHEMA)
    changed = 0
    with connection:
        for item in items:
            previous = connection.execute("SELECT * FROM company_status WHERE company_id=?",
                                          (item["id"],)).fetchone()
            change = change_for(previous, item)
            changed += bool(change)
            connection.execute(
                """INSERT INTO company_status(company_id, band, raise_announced, runway_today, change,
                   changed_at, updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(company_id) DO UPDATE SET band=excluded.band,
                   raise_announced=excluded.raise_announced, runway_today=excluded.runway_today,
                   change=COALESCE(excluded.change, company_status.change),
                   changed_at=COALESCE(excluded.changed_at, company_status.changed_at),
                   updated_at=excluded.updated_at""",
                (item["id"], item["band"], int("raise_announced" in item["flags"]), item["runway_today"],
                 change, _now() if change else None, _now()))
    return changed


def _collect_step(settings: Settings) -> dict:
    """Stegets källa väljs med RADAR_COLLECT_SOURCE; standard är fortfarande den gamla databasen."""
    if settings.collect_source == "mfn":
        return {"collect": collect_all(settings, log=lambda _line: None)}
    connection = connect(settings.db_path)
    try:
        return {"import": import_legacy(connection, settings.legacy_db_path)}
    finally:
        connection.close()


def _plans_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Hämta text för nya kallelser till extra stämma och bygg om signalerna om kapitalanskaffning."""
    try:
        connection = connect(settings.db_path)
        try:
            notices = plans.fetch_notices(connection, settings, log=log)
            return {"notices": notices, **plans.run(connection)}
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"plans misslyckades: {error}")
        return {"error": str(error)}


def _sectors_step(settings: Settings, client, log: Callable[[str], None]) -> dict:
    """ICB-bransch ur gamla databasen och underbransch för nya bolag (begränsat antal AI-anrop per varv)."""
    from . import sectors

    try:
        connection = connect(settings.db_path)
        try:
            result = {"icb": sectors.import_icb(connection, settings.legacy_db_path)}
            if client is not None:
                result["classify"] = sectors.classify(connection, client, limit=60, token_budget=80_000)
            return result
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"sectors misslyckades: {error}")
        return {"error": str(error)}


def _authorizations_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Läs stämmokommunikéer för emissionsbemyndiganden; ett fel här stoppar aldrig varvet."""
    try:
        connection = connect(settings.db_path)
        try:
            return authorizations.run(connection, settings, log=lambda _line: None)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"authorizations misslyckades: {error}")
        return {"error": str(error)}


def _fx_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Hämta dagens växelkurser (högst en gång per dag och valuta); ett fel stoppar aldrig varvet."""
    from . import fx

    try:
        connection = connect(settings.db_path)
        try:
            return fx.refresh(connection)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"växelkurser misslyckades: {error}")
        return {"error": str(error)}


def _market_history_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Kurssammandrag (högst en hämtning per bolag och dag, 150 bolag per varv); ett fel stoppar aldrig varvet."""
    from . import market

    try:
        connection = connect(settings.db_path)
        try:
            return market.run(connection, settings, limit=150, log=lambda _line: None)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"kurshistorik misslyckades: {error}")
        return {"error": str(error)}


def _insiders_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Insynshandel från FI (högst en hämtning per bolag och dag, 100 bolag per varv); fel stoppar aldrig varvet."""
    from . import insiders

    try:
        connection = connect(settings.db_path)
        try:
            return insiders.run(connection, settings, limit=100, log=lambda _line: None)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"insynshandel misslyckades: {error}")
        return {"error": str(error)}


def _owners_step(settings: Settings, client: ChatClient | None, log: Callable[[str], None]) -> dict:
    """Största ägare ur rapporttext (kräver AI-klient; högst 60 000 tokens per varv); fel stoppar aldrig varvet."""
    if client is None:
        return {"skipped": "ingen AI-klient"}
    from . import owners

    try:
        connection = connect(settings.db_path)
        try:
            return owners.run(connection, settings, client, limit=30, token_budget=OWNERS_TOKEN_BUDGET,
                              log=lambda _line: None)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"ägare misslyckades: {error}")
        return {"error": str(error)}


def _shorts_step(settings: Settings, log: Callable[[str], None]) -> dict:
    """Blankningsregistret från FI (högst en hämtning per dag); fel stoppar aldrig varvet."""
    from . import shorts

    try:
        connection = connect(settings.db_path)
        try:
            return shorts.run(connection, settings, log=lambda _line: None)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001
        log(f"blankning misslyckades: {error}")
        return {"error": str(error)}


def run_cycle(settings: Settings, client: ChatClient | None, *, report_budget: int, news_budget: int,
              backfill_budget: int = 0, log: Callable[[str], None] = logger.info) -> dict:
    from .web import latest_rows

    today = date.today().isoformat()
    summary: dict = {"started_at": _now()}
    with CycleLock(settings.db_path.with_suffix(".lock")):
        summary.update(_collect_step(settings))
        summary["reports"] = run_latest(settings, client, today=today, token_budget=report_budget,
                                        log=lambda _line: None)
        if client is not None:
            summary["news"] = run_news(settings, client, today=today, token_budget=news_budget,
                                       log=lambda _line: None)
            if backfill_budget:
                # Efter den löpande granskningen och med egen budget: historiken fylls på utan att tränga undan.
                from .news import run_backfill

                summary["backfill"] = run_backfill(settings, client, today=today, token_budget=backfill_budget,
                                                   log=lambda _line: None)
        summary["authorizations"] = _authorizations_step(settings, log)
        summary["plans"] = _plans_step(settings, log)   # efter stämmotexterna, som den läser
        summary["sectors"] = _sectors_step(settings, client, log)
        summary["fx"] = _fx_step(settings, log)
        connection = connect(settings.db_path)
        try:
            rows = latest_rows(connection)
            wanted = [(i["id"], isin) for i, isin in prospect_companies(connection, rows)]
            summary["market"] = refresh_many(connection, wanted, today)
            summary["changes"] = record_changes(connection, latest_rows(connection))
        finally:
            connection.close()
        summary["market_history"] = _market_history_step(settings, log)
        summary["insiders"] = _insiders_step(settings, log)
        summary["owners"] = _owners_step(settings, client, log)
        summary["shorts"] = _shorts_step(settings, log)
    summary["finished_at"] = _now()
    log(json.dumps(summary, ensure_ascii=False))
    return summary


def loop(settings: Settings, client: ChatClient | None, *, interval_minutes: float,
         report_budget: int, news_budget: int, backfill_budget: int = 0, first_delay: float = 120.0) -> None:
    time.sleep(first_delay)  # låt webben och deployen komma igång först
    while True:
        try:
            run_cycle(settings, client, report_budget=report_budget, news_budget=news_budget,
                      backfill_budget=backfill_budget)
        except FileExistsError:
            logger.warning("schedule_skipped: ett annat varv pågår; försöker igen om fem minuter")
            time.sleep(RETRY_AFTER_SKIP_SECONDS)
            continue
        except Exception:  # noqa: BLE001 - ett fel får inte stoppa driften
            logger.exception("schedule_cycle_failed")
        time.sleep(max(300.0, interval_minutes * 60))
