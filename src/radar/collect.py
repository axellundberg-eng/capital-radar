"""Insamling: hämta varje bevakat bolags MFN-sida, klassificera och spara nya poster.

Rapporter hamnar i `reports` (status new, PDF från första bilagan), kalenderposter i
`calendar_events` (se calendar.py) och allt annat i `news`. Redan kända poster (samma source_url, eller
samma bolag, rubrik och dag) hoppas över, även sådana som importerats ur den
gamla databasen. Ett bolag som misslyckas stoppar aldrig körningen.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import calendar, mfn
from .classify import MEETING, classify
from .db import connect, next_document_id
from .fetch import allowed, fetch_pdf
from .pdftext import pages as pdf_pages
from .importer import legacy_slugs, open_legacy
from .settings import Settings

logger = logging.getLogger(__name__)

# Rapporter med rubrik i nyhetsform och numeriskt filnamn (Cision: 4338911.pdf) känns bara igen på
# innehållet. Därför läses de första sidorna, men bara för bolag vars senaste rapport är gammal och bara
# för poster efter den rapporten. Varje PDF läses en gång (tabellen pdf_probes).
STALE_DAYS = 100
# Bara första sidan: en rapports förstasida säger vad den är, medan kallelser och kommentarer kan nämna
# "delårsrapport januari–juni" längre in.
PROBE_PAGES = 1
REPORT_TEXT = re.compile(
    r"delårsrapport|halvårsrapport|kvartalsrapport|bokslutskommunik|interim report|half[- ]year report"
    r"|quarterly report|year[- ]end report|(?:januari|january)\s*[–-]\s*(?:mars|march|juni|june|september|december)"
    r"|(?:juli|july|april|oktober|october)\s*[–-]\s*(?:september|juni|june|december)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def title_key(title: str | None) -> str:
    return " ".join((title or "").split()).casefold()


def is_known(connection: sqlite3.Connection, company_id: int, item: mfn.MfnItem,
             tables: tuple[str, ...] = ("reports", "news")) -> bool:
    """Finns posten redan, via källänk eller via bolag + rubrik + dag?"""
    day = item.published_at[:10]
    for table in tables:
        if connection.execute(f"SELECT 1 FROM {table} WHERE source_url=?", (item.url,)).fetchone():
            return True
        rows = connection.execute(
            f"SELECT title FROM {table} WHERE company_id=? AND substr(published_at, 1, 10)=?",
            (company_id, day)).fetchall()
        if any(title_key(row[0]) == title_key(item.title) for row in rows):
            return True
    return False


def insert_item(connection: sqlite3.Connection, company_id: int, item: mfn.MfnItem, table: str) -> bool:
    """Lägg in en ny post med eget id. Kontroll och insättning sker under samma skrivlås."""
    if connection.in_transaction:
        connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        # En rapport som tidigare sparats som nyhet (rubrik i nyhetsform) läggs ändå till som rapport.
        if is_known(connection, company_id, item, ("reports",) if table == "reports" else ("reports", "news")):
            connection.rollback()
            return False
        document_id = next_document_id(connection)
        if table == "reports":
            connection.execute(
                """INSERT INTO reports(document_id, company_id, title, published_at, pdf_url, source_url,
                   attachments, status, updated_at) VALUES (?,?,?,?,?,?,?, 'new', ?)""",
                (document_id, company_id, item.title, item.published_at, item.pdf_url, item.url,
                 json.dumps(list(item.attachments)), _now()))
        else:
            connection.execute(
                """INSERT INTO news(document_id, company_id, title, published_at, url, source_url, status,
                   updated_at) VALUES (?,?,?,?,?,?, 'new', ?)""",
                (document_id, company_id, item.title, item.published_at, item.url, item.url, _now()))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def _pdfs(attachments) -> list[str]:
    return [url for url in attachments or () if url.lower().split("?")[0].endswith(".pdf")]


def store_attachments(connection: sqlite3.Connection, item: mfn.MfnItem) -> bool:
    """Spara bilagorna för en känd rapport som saknar dem. En överhoppad rapport (första PDF:en var bara
    pressmeddelandet) med fler PDF:er får ett nytt försök. Sant om rapporten ska läsas om."""
    with connection:
        changed = connection.execute(
            "UPDATE reports SET attachments=? WHERE source_url=? AND attachments IS NULL",
            (json.dumps(list(item.attachments)), item.url)).rowcount
        if changed and len(_pdfs(item.attachments)) > 1:
            return connection.execute(
                "UPDATE reports SET status='new', error=NULL WHERE source_url=? AND status='skipped'",
                (item.url,)).rowcount > 0
    return False


def _stale_since(connection: sqlite3.Connection, company_id: int, today: str | None = None) -> str | None:
    """Senaste rapportens publiceringsdag om den är äldre än STALE_DAYS (eller "0000" om bolaget saknar
    rapporter), annars None."""
    row = connection.execute(
        """SELECT MAX(substr(published_at, 1, 10)) FROM reports WHERE company_id=?
           AND status NOT IN ('skipped', 'failed')""", (company_id,)).fetchone()
    latest = row[0] if row else None
    if not latest:
        return "0000"
    day = datetime.fromisoformat(today[:10]) if today else datetime.now(timezone.utc)
    cutoff = (day - timedelta(days=STALE_DAYS)).date().isoformat()
    return latest if latest < cutoff else None


def probe_is_report(connection: sqlite3.Connection, settings: Settings, url: str,
                    fetch: Callable = fetch_pdf, read: Callable = pdf_pages) -> bool:
    """Läs de första sidorna i en PDF en gång och avgör om den är en rapport."""
    known = connection.execute("SELECT is_report FROM pdf_probes WHERE url=?", (url,)).fetchone()
    if known is not None:
        return bool(known[0])
    if not allowed(url):
        return False
    with tempfile.TemporaryDirectory() as folder:
        try:
            path, _sha = fetch(url, Path(folder) / "probe.pdf", timeout=min(settings.http_timeout, 60.0),
                               user_agent=settings.user_agent)
            text = " ".join(read(path)[:PROBE_PAGES])
        except Exception as error:  # noqa: BLE001 - en trasig bilaga får inte stoppa insamlingen
            logger.info("pdf-prov %s: %s", url, error)
            return False   # sparas inte: nytt försök nästa varv
    is_report = bool(REPORT_TEXT.search(text))
    with connection:
        connection.execute("INSERT OR REPLACE INTO pdf_probes(url, is_report, checked_at) VALUES (?,?,?)",
                           (url, int(is_report), _now()))
    return is_report


def collect_company(connection: sqlite3.Connection, settings: Settings, company, *,
                    throttle: mfn.Throttle | None = None, probe: Callable | None = None,
                    today: str | None = None) -> dict[str, int]:
    """Hämta ett bolags sida och spara nya poster. `company` har id, name och mfn_slug."""
    items = mfn.fetch_items(company["mfn_slug"], settings, throttle)
    counts = {"fetched": len(items), "new_reports": 0, "new_news": 0, "calendar": 0, "probed_reports": 0,
              "retry_reports": 0}
    probe = probe or (lambda url: probe_is_report(connection, settings, url))
    stale = _stale_since(connection, company["id"], today)
    for item in items:
        kind = classify(item.title, item.category, item.attachments)
        if kind == "calendar":
            counts["calendar"] += calendar.store_item(connection, company["id"], item)
            continue
        if (kind == "other" and stale and item.pdf_url and item.published_at[:10] > stale
                and not MEETING.search(item.title) and not re.search(r"kallelse|notice", item.title, re.I)
                and not is_known(connection, company["id"], item, ("reports",)) and probe(item.pdf_url)):
            kind = "report"
            counts["probed_reports"] += 1
        table = "reports" if kind == "report" else "news"
        if insert_item(connection, company["id"], item, table):
            counts["new_reports" if table == "reports" else "new_news"] += 1
        elif table == "reports" and store_attachments(connection, item):
            counts["retry_reports"] += 1
    return counts


def snapshot_db(settings: Settings, directory: Path) -> Settings:
    """Lättviktskopia av bolag och kända poster för torrkörning; radar.db rörs inte.

    Slugar som saknas fylls i ur den gamla databasen (skrivskyddat), om den finns.
    """
    target = connect(directory / "snapshot.db")
    if settings.db_path.exists():
        source = sqlite3.connect(f"file:{settings.db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            _copy_known(source, target)
        finally:
            source.close()
    if settings.legacy_db_path.exists():
        legacy = open_legacy(settings.legacy_db_path)
        try:
            with target:
                target.executemany("UPDATE companies SET mfn_slug=? WHERE id=? AND mfn_slug IS NULL",
                                   [(slug, issuer_id) for issuer_id, slug in legacy_slugs(legacy).items()])
        finally:
            legacy.close()
    target.close()
    return replace(settings, db_path=directory / "snapshot.db")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _copy_known(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    for table, wanted in (("companies", ("id", "name", "mfn_slug")),
                          ("reports", ("document_id", "company_id", "title", "published_at", "source_url")),
                          ("news", ("document_id", "company_id", "title", "published_at", "source_url"))):
        names = [name for name in wanted if name in _columns(source, table)]
        rows = source.execute(f"SELECT {', '.join(names)} FROM {table}").fetchall()
        with target:
            target.executemany(
                f"INSERT OR IGNORE INTO {table}({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", rows)


class _Run:
    """Delat tillstånd för en körning: en fördröjning per arbetstråd och en anslutning per bolag."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.throttle = mfn.Throttle()

    def work(self, company) -> dict[str, int]:
        connection = connect(self.settings.db_path)   # öppnas och stängs i arbetstråden
        try:
            return collect_company(connection, self.settings, company, throttle=self.throttle)
        finally:
            connection.close()


def _companies(settings: Settings, limit: int | None) -> tuple[list[sqlite3.Row], int]:
    connection = connect(settings.db_path)
    try:
        rows = connection.execute(
            "SELECT id, name, mfn_slug FROM companies WHERE mfn_slug IS NOT NULL ORDER BY id").fetchall()
        missing = connection.execute("SELECT COUNT(*) FROM companies WHERE mfn_slug IS NULL").fetchone()[0]
    finally:
        connection.close()
    return (rows[:limit] if limit else rows), int(missing)


def collect_all(settings: Settings, workers: int = 4, log: Callable[[str], None] = logger.info, *,
                limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    """Samla alla bolag. Med `dry_run` skrivs allt till en tillfällig kopia i stället för radar.db."""
    with working_settings(settings, dry_run) as work_settings:
        companies, missing = _companies(work_settings, limit)
        counts = {"companies": len(companies), "fetched": 0, "new_reports": 0, "new_news": 0, "calendar": 0,
                  "probed_reports": 0, "retry_reports": 0, "failed": 0, "skipped_no_slug": missing}
        run = _Run(work_settings)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(run.work, company): company for company in companies}
            for future in as_completed(futures):
                _tally(counts, futures[future], future, log)
    log(f"collect: {counts}")
    return counts


def _tally(counts: dict, company, future, log: Callable[[str], None]) -> None:
    try:
        result = future.result()
    except Exception as error:  # noqa: BLE001 - ett bolag får inte stoppa körningen
        counts["failed"] += 1
        log(f"collect: {company['name']} ({company['mfn_slug']}) misslyckades: {error}")
        return
    for key, value in result.items():
        counts[key] += value
    if result["new_reports"] or result["new_news"]:
        log(f"collect: {company['name']}: {result['new_reports']} rapporter, {result['new_news']} nyheter")


@contextmanager
def working_settings(settings: Settings, dry_run: bool) -> Iterator[Settings]:
    """Riktiga inställningar, eller för torrkörning en tillfällig kopia som tas bort efteråt."""
    if not dry_run:
        yield settings
        return
    directory = Path(tempfile.mkdtemp(prefix="radar-dry-"))
    try:
        yield snapshot_db(settings, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def known_pairs(connection: sqlite3.Connection) -> Iterator[tuple[int, str, str, str | None]]:
    """(bolag, rubrik, dag, källänk) för alla kända poster; används av jämförelseläget."""
    for table in ("reports", "news"):
        for row in connection.execute(f"SELECT company_id, title, published_at, source_url FROM {table}"):
            yield row[0], title_key(row[1]), (row[2] or "")[:10], row[3]
