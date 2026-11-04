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
    type TEXT NOT NULL,                -- rights_issue, directed_issue, loan, convertible, warrants, other
    status TEXT NOT NULL,              -- announced, completed, cancelled
    gross_amount REAL, net_amount REAL, -- hela valutaenheter
    currency TEXT,
    event_date TEXT NOT NULL,
    quote TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS financing_company ON financing_events(company_id, event_date);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    return connection
