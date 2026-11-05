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


def run_cycle(settings: Settings, client: ChatClient | None, *, report_budget: int, news_budget: int,
              log: Callable[[str], None] = logger.info) -> dict:
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
        connection = connect(settings.db_path)
        try:
            rows = latest_rows(connection)
            wanted = [(i["id"], isin) for i, isin in prospect_companies(connection, rows)]
            summary["market"] = refresh_many(connection, wanted, today)
            summary["changes"] = record_changes(connection, latest_rows(connection))
        finally:
            connection.close()
    summary["finished_at"] = _now()
    log(json.dumps(summary, ensure_ascii=False))
    return summary


def loop(settings: Settings, client: ChatClient | None, *, interval_minutes: float,
         report_budget: int, news_budget: int, first_delay: float = 120.0) -> None:
    time.sleep(first_delay)  # låt webben och deployen komma igång först
    while True:
        try:
            run_cycle(settings, client, report_budget=report_budget, news_budget=news_budget)
        except FileExistsError:
            logger.warning("schedule_skipped: ett annat varv pågår; försöker igen om fem minuter")
            time.sleep(RETRY_AFTER_SKIP_SECONDS)
            continue
        except Exception:  # noqa: BLE001 - ett fel får inte stoppa driften
            logger.exception("schedule_cycle_failed")
        time.sleep(max(300.0, interval_minutes * 60))
