"""Jämförelseläge: hittar den nya insamlaren det som den gamla redan har samlat in?

Läser den gamla databasen skrivskyddat (bara data) och jämför dokumenten från de
senaste dagarna med bolagens MFN-sidor och med det som redan finns i radar.db.
Ingenting skrivs till radar.db.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Callable

from . import mfn
from .classify import classify
from .collect import known_pairs, title_key, working_settings
from .db import connect
from .importer import legacy_slugs, open_legacy
from .settings import Settings

MAX_EXAMPLES = 15


def legacy_documents(settings: Settings, since: str) -> list[dict]:
    source = open_legacy(settings.legacy_db_path)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            """SELECT d.id, d.issuer_id, i.canonical_name AS company, d.title, d.published_at, d.canonical_url,
                      d.profile_category FROM documents d JOIN issuers i ON i.id = d.issuer_id
               WHERE substr(d.published_at, 1, 10) >= ? ORDER BY d.published_at DESC""", (since,)).fetchall()
    finally:
        source.close()
    return [dict(row) for row in rows]


def _all_slugs(settings: Settings, snapshot: Settings) -> dict[int, str]:
    """Slugar ur kopian av radar.db, med den gamla databasens länkar som reserv."""
    source = open_legacy(settings.legacy_db_path)
    try:
        slugs = legacy_slugs(source)
    finally:
        source.close()
    connection = connect(snapshot.db_path)
    try:
        slugs.update({row["id"]: row["mfn_slug"] for row in connection.execute(
            "SELECT id, mfn_slug FROM companies WHERE mfn_slug IS NOT NULL")})
        return slugs
    finally:
        connection.close()


def _fetch_pages(settings: Settings, slugs: dict[int, str], workers: int,
                 log: Callable[[str], None]) -> tuple[dict[int, list[mfn.MfnItem]], int]:
    throttle = mfn.Throttle()

    def one(entry):
        company_id, slug = entry
        try:
            return company_id, mfn.fetch_items(slug, settings, throttle)
        except Exception as error:  # noqa: BLE001 - ett bolag får inte stoppa jämförelsen
            log(f"compare: {slug} misslyckades: {error}")
            return company_id, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(one, slugs.items()))
    return {key: items for key, items in results if items is not None}, sum(1 for _, items in results if items is None)


def _found_keys(pages: dict[int, list[mfn.MfnItem]], stored: list[tuple]) -> tuple[set, set]:
    urls = {item.url for items in pages.values() for item in items}
    keys = {(company_id, title_key(item.title), item.published_at[:10])
            for company_id, items in pages.items() for item in items}
    for company_id, title, day, source_url in stored:
        keys.add((company_id, title, day))
        if source_url:
            urls.add(source_url)
    return urls, keys


def _matches(document: dict, urls: set, keys: set) -> bool:
    url = mfn.normalize_url(document["canonical_url"]) if document["canonical_url"] else None
    key = (document["issuer_id"], title_key(document["title"]), (document["published_at"] or "")[:10])
    return (url in urls) or (key in keys)


def _percent(matched: int, total: int) -> float:
    return round(100.0 * matched / total, 1) if total else 0.0


def compare_legacy(settings: Settings, days: int, *, workers: int = 4, today: date | None = None,
                   log: Callable[[str], None] = print) -> dict:
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    documents = legacy_documents(settings, since)
    with working_settings(settings, True) as snapshot:
        slugs = _all_slugs(settings, snapshot)
        connection = connect(snapshot.db_path)
        try:
            stored = list(known_pairs(connection))
        finally:
            connection.close()
    wanted = {doc["issuer_id"]: slugs[doc["issuer_id"]] for doc in documents if doc["issuer_id"] in slugs}
    pages, failed = _fetch_pages(settings, wanted, workers, log)
    urls, keys = _found_keys(pages, stored)
    return _summary(documents, urls, keys, pages, since, failed, len(wanted))


def _summary(documents: list[dict], urls: set, keys: set, pages: dict, since: str, failed: int,
             companies: int) -> dict:
    groups = {"report": [0, 0], "other": [0, 0]}
    classified_ok = misses_total = 0
    misses: list[dict] = []
    for doc in documents:
        group = "report" if doc["profile_category"] == "report" else "other"
        found = _matches(doc, urls, keys)
        groups[group][0] += 1
        groups[group][1] += found
        classified_ok += found and group == "report" and classify(doc["title"] or "") == "report"
        if not found:
            misses_total += 1
            if len(misses) < MAX_EXAMPLES:
                misses.append({key: doc[key] for key in ("id", "company", "title", "published_at", "profile_category")})
    total = len(documents)
    return {
        "since": since, "legacy_documents": total, "matched": total - misses_total,
        "matched_percent": _percent(total - misses_total, total),
        "reports": {"total": groups["report"][0], "matched": groups["report"][1],
                    "percent": _percent(groups["report"][1], groups["report"][0]),
                    "classified_as_report": classified_ok},
        "other": {"total": groups["other"][0], "matched": groups["other"][1],
                  "percent": _percent(groups["other"][1], groups["other"][0])},
        "companies_fetched": len(pages), "companies_failed": failed,
        "companies_without_slug": len({d["issuer_id"] for d in documents}) - companies,
        "pages_shorter_than_window": sum(1 for items in pages.values() if min(i.published_at[:10] for i in items) > since),
        "misses": misses,
    }
