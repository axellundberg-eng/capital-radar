"""Databasen för den nya kärnan: få tabeller, status per steg."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,            -- samma id som i den gamla databasen
    name TEXT NOT NULL,
    isin TEXT, symbol TEXT, market TEXT, segment TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    document_id INTEGER PRIMARY KEY,   -- samma id som i den gamla databasen
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    published_at TEXT,
    pdf_url TEXT,
    status TEXT NOT NULL DEFAULT 'new', -- new, fetched, text, extracted, failed
    error TEXT,
    pdf_path TEXT, pdf_sha256 TEXT, page_count INTEGER,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS reports_company ON reports(company_id, published_at);
CREATE TABLE IF NOT EXISTS report_pages (
    document_id INTEGER NOT NULL REFERENCES reports(document_id),
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (document_id, page)
);
CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES reports(document_id),
    model TEXT NOT NULL,
    pages TEXT NOT NULL,               -- JSON-lista med skickade sidor
    status TEXT NOT NULL,              -- ok, invalid, failed
    error TEXT,
    response_json TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL REFERENCES extractions(id),
    document_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- cash, operating_cash_flow, capex, investing_cash_flow
    value REAL NOT NULL,               -- hela valutaenheter (kronor, euro)
    currency TEXT,
    period_start TEXT, period_end TEXT,
    page INTEGER, quote TEXT
);
CREATE INDEX IF NOT EXISTS facts_document ON facts(document_id);
CREATE TABLE IF NOT EXISTS runway_results (
    company_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (company_id, document_id)
);
CREATE TABLE IF NOT EXISTS news (
    document_id INTEGER PRIMARY KEY,   -- samma id som i den gamla databasen
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    published_at TEXT,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'new', -- new, checked, financing, failed
    error TEXT,
    text TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS news_company ON news(company_id, published_at);
CREATE TABLE IF NOT EXISTS financing_events (
    id INTEGER PRIMARY KEY,
    news_document_id INTEGER NOT NULL REFERENCES news(document_id),
    company_id INTEGER NOT NULL,
    type TEXT NOT NULL,                -- rights_issue, directed_issue, bridge_loan, loan, credit_facility, convertible, warrants, repayment, other
    status TEXT NOT NULL,              -- announced, completed, cancelled
    gross_amount REAL, net_amount REAL, -- hela valutaenheter; vid kreditfacilitet är gross ramen
    drawn_amount REAL,                 -- utnyttjad del av en kreditfacilitet, om den anges
    currency TEXT,
    event_date TEXT NOT NULL,
    quote TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS financing_company ON financing_events(company_id, event_date);
CREATE TABLE IF NOT EXISTS company_events (
    id INTEGER PRIMARY KEY,
    news_document_id INTEGER NOT NULL REFERENCES news(document_id),
    company_id INTEGER NOT NULL,
    type TEXT NOT NULL,                -- reverse_acquisition, merger, acquisition, name_change, public_offer, delisting, bankruptcy, reorganisation, liquidation, spin_off, other
    status TEXT NOT NULL,              -- announced, completed, cancelled
    event_date TEXT NOT NULL,
    counterparty TEXT,
    quote TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS company_events_company ON company_events(company_id, event_date);
CREATE TABLE IF NOT EXISTS market_data (
    company_id INTEGER PRIMARY KEY,
    isin TEXT,
    market_cap REAL,                   -- hela valutaenheter
    currency TEXT,
    source TEXT,                       -- nasdaq
    fetched_at TEXT NOT NULL,
    error TEXT,
    sector TEXT                        -- Nasdaqs sektornamn (ICB), när det finns
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


DOCUMENT_ID_FLOOR = 10_000_000   # egna dokument-id börjar här, så de aldrig krockar med gamla id


def _add_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        with connection:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as error:  # en annan arbetare hann före
        if "duplicate column" not in str(error):
            raise


def _migrate(connection: sqlite3.Connection) -> None:
    """Lägg till kolumner som tillkommit efter att databasen skapades (även för nya databaser)."""
    _add_column(connection, "financing_events", "drawn_amount", "REAL")
    # Strukturella händelser (events.py): NULL = nyheten är aldrig bedömd, annars checked/structural/failed.
    _add_column(connection, "news", "events_status", "TEXT")
    _add_column(connection, "news", "events_error", "TEXT")
    _add_column(connection, "news", "events_tokens", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "companies", "mfn_slug", "TEXT")   # bolagets sida: mfn.se/all/a/<slug>
    for table in ("reports", "news"):
        _add_column(connection, table, "source_url", "TEXT")   # postens sida på MFN, unik per tabell
        with connection:
            connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_source_url ON {table}(source_url)")


def next_document_id(connection: sqlite3.Connection) -> int:
    """Nästa lediga dokument-id: högst av största befintliga id och 10 000 000, plus ett.

    Reports och news delar id-rymd (som gamla documents). Anropa inom
    `BEGIN IMMEDIATE` så att två arbetare inte får samma id.
    """
    row = connection.execute(
        """SELECT MAX(COALESCE((SELECT MAX(document_id) FROM reports), 0),
                      COALESCE((SELECT MAX(document_id) FROM news), 0), ?)""",
        (DOCUMENT_ID_FLOOR,)).fetchone()
    return int(row[0]) + 1
