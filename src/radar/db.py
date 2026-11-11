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
CREATE TABLE IF NOT EXISTS corrections (   -- rättelser från granskningen, se review.py
    id INTEGER PRIMARY KEY,
    target TEXT NOT NULL,              -- fact eller event
    company_id INTEGER NOT NULL,
    document_id INTEGER,               -- rapporten (fact)
    event_id INTEGER,                  -- financing_events.id (event)
    kind TEXT, period_start TEXT, period_end TEXT,
    field TEXT NOT NULL,
    old_value TEXT, new_value TEXT,
    reason TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL,
    reverted_at TEXT                   -- satt = ångrad, räknas inte längre
);
CREATE INDEX IF NOT EXISTS corrections_document ON corrections(target, document_id);
CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT, finished_at TEXT, since TEXT,
    checked INTEGER, corrections INTEGER, findings_before INTEGER, findings_after INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS fx_rates (      -- Riksbankens dagskurser, se fx.py
    currency TEXT NOT NULL,            -- EUR, USD, ...
    rate_date TEXT NOT NULL,
    sek_per_unit REAL NOT NULL,        -- så många kronor för en enhet av valutan
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (currency, rate_date)
);
CREATE TABLE IF NOT EXISTS calendar_events (   -- kalenderposter från MFN: rapportdatum, stämmor, se calendar.py
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    source_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published_at TEXT,
    event_date TEXT,                   -- datumet posten aviserar (YYYY-MM-DD), om det gick att läsa
    kind TEXT NOT NULL,                -- report, meeting, other
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calendar_company ON calendar_events(company_id, event_date);
CREATE TABLE IF NOT EXISTS authorizations (    -- bemyndiganden att emittera aktier, se authorizations.py
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    news_document_id INTEGER NOT NULL UNIQUE,
    meeting_date TEXT,
    max_dilution_pct REAL,             -- högst så många procent av aktierna, om det anges
    max_shares REAL,                   -- högst så många aktier, om det anges
    directed_allowed INTEGER,          -- 1 om avvikelse från företrädesrätt är tillåten
    quote TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS authorizations_company ON authorizations(company_id, meeting_date);
CREATE TABLE IF NOT EXISTS financing_signals (   -- stämmobeslut och uttalade planer om kapitalanskaffning, se plans.py
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- stamma_issue | plan | evaluating
    source TEXT NOT NULL,              -- report | news
    document_id INTEGER NOT NULL,
    published_at TEXT,
    timing TEXT,                       -- t.ex. "under första kvartalet 2027", om det anges
    quote TEXT,
    url TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, document_id)
);
CREATE INDEX IF NOT EXISTS financing_signals_company ON financing_signals(company_id, published_at);
CREATE TABLE IF NOT EXISTS market_history_summary (   -- kurshistorik i sammandrag per bolag, se market.py
    company_id INTEGER PRIMARY KEY,
    isin TEXT, order_book TEXT,        -- Nasdaqs orderboks-id, sparas så att nästa hämtning kräver ett anrop
    currency TEXT,
    price REAL, price_change_1m_pct REAL, price_change_3m_pct REAL, price_change_12m_pct REAL,
    avg_daily_turnover_3m REAL,        -- SEK per handelsdag (stängningskurs x volym)
    avg_daily_volume_3m REAL,          -- antal aktier per handelsdag
    high_52w REAL, low_52w REAL,
    as_of TEXT, source TEXT, fetched_at TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS insider_trades (   -- Finansinspektionens insynsregister, se insiders.py
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    published TEXT, transaction_date TEXT,
    person TEXT, role TEXT, transaction_type TEXT,   -- karaktär: Förvärv, Avyttring, ...
    volume REAL, price REAL, currency TEXT, value REAL,   -- value = volym x pris i transaktionens valuta
    instrument TEXT,
    source_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS insider_company ON insider_trades(company_id, transaction_date);
CREATE TABLE IF NOT EXISTS insider_fetch (   -- senaste hämtning per bolag (högst en per dag)
    company_id INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,
    unmatched INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS shareholders (   -- största ägare ur rapporttext, se owners.py
    company_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    as_of TEXT,
    rank INTEGER NOT NULL,
    name TEXT NOT NULL,
    shares REAL, pct_capital REAL, pct_votes REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (company_id, document_id, rank)
);
CREATE TABLE IF NOT EXISTS owner_fetch (   -- senaste ägartolkning per bolag
    company_id INTEGER PRIMARY KEY,
    document_id INTEGER,               -- rapporten som tolkades (NULL = ingen rapport med ägartabell)
    status TEXT NOT NULL,              -- ok, no_table, failed
    error TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS short_positions (   -- FI:s blankningsregister, aktuell ögonblicksbild, se shorts.py
    company_id INTEGER NOT NULL,
    holder TEXT NOT NULL,
    pct REAL NOT NULL,
    position_date TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS short_positions_company ON short_positions(company_id);
CREATE TABLE IF NOT EXISTS short_fetch (   -- senaste hämtning av blankningsregistret (högst en per dag)
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0, error TEXT
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
    _add_column(connection, "financing_events", "advisers", "TEXT")
    # Villkor och utfall per emission (financing.py): garanti, teckning, pris, utspädning, TO-villkor.
    for column, ddl in (("subscription_commitments", "REAL"), ("guarantee_amount", "REAL"),
                        ("guarantee_fee_pct", "REAL"), ("guarantors", "TEXT"), ("subscription_rate_pct", "REAL"),
                        ("guarantee_utilized", "REAL"), ("issue_price", "REAL"), ("shares_issued", "REAL"),
                        ("shares_before", "REAL"), ("discount_pct", "REAL"), ("exercise_price", "REAL"),
                        ("exercise_start", "TEXT"), ("exercise_end", "TEXT"), ("issue_costs", "REAL")):
        _add_column(connection, "financing_events", column, ddl)
    # Strukturella händelser (events.py): NULL = nyheten är aldrig bedömd, annars checked/structural/failed.
    _add_column(connection, "news", "events_status", "TEXT")
    _add_column(connection, "news", "events_error", "TEXT")
    _add_column(connection, "news", "events_tokens", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "companies", "mfn_slug", "TEXT")   # bolagets sida: mfn.se/all/a/<slug>
    # Alla bilagor (JSON-lista). Första PDF:en är ibland bara pressmeddelandet; då provas övriga.
    _add_column(connection, "reports", "attachments", "TEXT")
    with connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS pdf_probes (   -- PDF:er som lästs för att avgöra om de är rapporter
                   url TEXT PRIMARY KEY, is_report INTEGER NOT NULL, checked_at TEXT NOT NULL)""")
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
