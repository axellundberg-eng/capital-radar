"""Engångsimport av data (inte kod) ur den gamla databasen, skrivskyddat."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def import_legacy(target: sqlite3.Connection, legacy_db: Path) -> dict[str, int]:
    source = sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        companies = source.execute(
            """SELECT i.id, i.canonical_name, s.isin, s.symbol, s.market, s.segment
               FROM issuers i LEFT JOIN securities s ON s.id = (
                   SELECT id FROM securities WHERE issuer_id=i.id ORDER BY active DESC, id LIMIT 1)"""
        ).fetchall()
        reports = source.execute(
            """SELECT d.id, d.issuer_id, d.title, d.published_at,
                      (SELECT url FROM attachments a WHERE a.document_id=d.id
                       AND lower(a.url) LIKE '%.pdf%' ORDER BY a.id LIMIT 1)
               FROM documents d WHERE d.profile_category='report' AND d.issuer_id IS NOT NULL"""
        ).fetchall()
        news = source.execute(
            """SELECT d.id, d.issuer_id, d.title, d.published_at, d.canonical_url FROM documents d
               WHERE (d.profile_category IS NULL OR d.profile_category != 'report')
               AND d.issuer_id IN (SELECT id FROM issuers)""").fetchall()
    finally:
        source.close()
    with target:
        target.executemany(
            """INSERT INTO companies(id, name, isin, symbol, market, segment) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, isin=excluded.isin,
               symbol=excluded.symbol, market=excluded.market, segment=excluded.segment""", companies)
        target.executemany(
            """INSERT INTO reports(document_id, company_id, title, published_at, pdf_url) VALUES (?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET pdf_url=COALESCE(reports.pdf_url, excluded.pdf_url)""",
            reports)
        target.executemany(
            """INSERT INTO news(document_id, company_id, title, published_at, url) VALUES (?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET company_id=excluded.company_id, title=excluded.title,
               published_at=excluded.published_at, url=COALESCE(excluded.url, news.url)""",
            [(row[0], row[1], row[2] or "", row[3], row[4]) for row in news])
    return {"companies": len(companies), "reports": len(reports),
            "reports_with_pdf": sum(1 for row in reports if row[4]), "news": len(news)}
