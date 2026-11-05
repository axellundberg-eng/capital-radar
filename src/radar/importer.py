"""Engångsimport av data (inte kod) ur den gamla databasen, skrivskyddat."""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from .mfn import normalize_url, slug_from_url


def open_legacy(legacy_db: Path) -> sqlite3.Connection:
    """Den gamla databasen, endast läsning."""
    return sqlite3.connect(f"file:{legacy_db.as_posix()}?mode=ro", uri=True, timeout=10)


def legacy_slugs(source: sqlite3.Connection) -> dict[int, str]:
    """MFN-slug per bolag, härledd ur dokumentens länkar (vanligaste sluggen vinner)."""
    found: dict[int, Counter] = {}
    for issuer_id, url in source.execute(
            "SELECT issuer_id, canonical_url FROM documents WHERE issuer_id IS NOT NULL AND canonical_url LIKE '%mfn.se%'"):
        slug = slug_from_url(url)
        if slug:
            found.setdefault(issuer_id, Counter())[slug] += 1
    return {issuer_id: counts.most_common(1)[0][0] for issuer_id, counts in found.items()}


def _backfill_urls(target: sqlite3.Connection, table: str, rows: list[tuple]) -> None:
    """Sätt source_url på importerade rader (id, url), så att insamlaren inte lägger in dubbletter."""
    values = [(normalize_url(url), document_id, normalize_url(url)) for document_id, url in rows if url]
    target.executemany(
        f"""UPDATE {table} SET source_url=? WHERE document_id=? AND source_url IS NULL
            AND NOT EXISTS (SELECT 1 FROM {table} WHERE source_url=?)""", values)


def import_legacy(target: sqlite3.Connection, legacy_db: Path) -> dict[str, int]:
    source = open_legacy(legacy_db)
    try:
        slugs = legacy_slugs(source)
        companies = source.execute(
            """SELECT i.id, i.canonical_name, s.isin, s.symbol, s.market, s.segment
               FROM issuers i LEFT JOIN securities s ON s.id = (
                   SELECT id FROM securities WHERE issuer_id=i.id ORDER BY active DESC, id LIMIT 1)"""
        ).fetchall()
        reports = source.execute(
            """SELECT d.id, d.issuer_id, d.title, d.published_at,
                      (SELECT url FROM attachments a WHERE a.document_id=d.id
                       AND lower(a.url) LIKE '%.pdf%' ORDER BY a.id LIMIT 1), d.canonical_url
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
            [row[:5] for row in reports])
        target.executemany(
            """INSERT INTO news(document_id, company_id, title, published_at, url) VALUES (?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET company_id=excluded.company_id, title=excluded.title,
               published_at=excluded.published_at, url=COALESCE(excluded.url, news.url)""",
            [(row[0], row[1], row[2] or "", row[3], row[4]) for row in news])
        _backfill_urls(target, "reports", [(row[0], row[5]) for row in reports])
        _backfill_urls(target, "news", [(row[0], row[4]) for row in news])
        target.executemany("UPDATE companies SET mfn_slug=? WHERE id=? AND mfn_slug IS NULL",
                           [(slug, issuer_id) for issuer_id, slug in slugs.items()])
    return {"companies": len(companies), "reports": len(reports),
            "reports_with_pdf": sum(1 for row in reports if row[4]), "news": len(news),
            "mfn_slugs": len(slugs)}
