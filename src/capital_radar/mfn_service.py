"""Service-lager för MFN-nyhetsendpoints.

Hämtar nyheter från documents-tabellen med MFN-källa.
"""

from __future__ import annotations

import sqlite3
from typing import Any


# Tillåtna värden för filter
VALID_PROFILE_CATEGORIES = {"report", "financing", "other", "corporate", "commercial_deal"}
VALID_ANALYSIS_STATUS = {"metadata_only", "extracted", "content_ready", "needs_ocr", "content_failed"}


def _build_mfn_query(
    issuer_id: int | None = None,
    profile_category: str | None = None,
    cash_relevance_min: int | None = None,
    analysis_status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[str, list[Any]]:
    """Bygg SQL-query för MFN-nyheter med filter."""
    conditions = ["ds.source_id = 'mfn'"]
    params: list[Any] = []

    if issuer_id is not None:
        conditions.append("d.issuer_id = ?")
        params.append(issuer_id)

    if profile_category:
        conditions.append("d.profile_category = ?")
        params.append(profile_category)

    if cash_relevance_min is not None:
        conditions.append("d.cash_relevance_score >= ?")
        params.append(cash_relevance_min)

    if analysis_status:
        conditions.append("d.analysis_status = ?")
        params.append(analysis_status)

    if from_date:
        conditions.append("d.published_at >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("d.published_at <= ?")
        params.append(to_date)

    if cursor:
        # Cursor-baserad pagination: published_at < cursor (DESC-sortering)
        conditions.append("d.published_at < ?")
        params.append(cursor)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT
            d.id,
            d.issuer_id,
            i.canonical_name AS issuer_name,
            d.title,
            d.published_at,
            d.category,
            d.profile_category,
            d.cash_relevance_score,
            d.relevance_reason,
            d.analysis_status,
            d.canonical_url,
            dc.page_count,
            dc.char_count
        FROM documents d
        JOIN document_sources ds ON ds.document_id = d.id
        JOIN issuers i ON i.id = d.issuer_id
        LEFT JOIN document_contents dc ON dc.document_id = d.id
        WHERE {where_clause}
        ORDER BY d.published_at DESC, d.id DESC
        LIMIT ?
    """
    params.append(limit + 1)  # +1 för att kolla om det finns fler

    return query, params


def _count_mfn_query(
    issuer_id: int | None = None,
    profile_category: str | None = None,
    cash_relevance_min: int | None = None,
    analysis_status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[str, list[Any]]:
    """Bygg COUNT-query för total_filtered."""
    conditions = ["ds.source_id = 'mfn'"]
    params: list[Any] = []

    if issuer_id is not None:
        conditions.append("d.issuer_id = ?")
        params.append(issuer_id)

    if profile_category:
        conditions.append("d.profile_category = ?")
        params.append(profile_category)

    if cash_relevance_min is not None:
        conditions.append("d.cash_relevance_score >= ?")
        params.append(cash_relevance_min)

    if analysis_status:
        conditions.append("d.analysis_status = ?")
        params.append(analysis_status)

    if from_date:
        conditions.append("d.published_at >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("d.published_at <= ?")
        params.append(to_date)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT COUNT(*) AS total
        FROM documents d
        JOIN document_sources ds ON ds.document_id = d.id
        WHERE {where_clause}
    """

    return query, params


def get_mfn_news(
    connection: sqlite3.Connection,
    issuer_id: int | None = None,
    profile_category: str | None = None,
    cash_relevance_min: int | None = None,
    analysis_status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Hämta MFN-nyheter med filter och pagination.

    Returns:
        {
            "items": [...],
            "next_cursor": "..." | None,
            "total_filtered": int
        }
    """
    # Validera limit
    limit = max(1, min(100, limit))

    # Validera enum-värden
    if profile_category and profile_category not in VALID_PROFILE_CATEGORIES:
        raise ValueError(f"Ogiltig profile_category: {profile_category}")
    if analysis_status and analysis_status not in VALID_ANALYSIS_STATUS:
        raise ValueError(f"Ogiltig analysis_status: {analysis_status}")

    # Hämta total (utan cursor)
    count_query, count_params = _count_mfn_query(
        issuer_id=issuer_id,
        profile_category=profile_category,
        cash_relevance_min=cash_relevance_min,
        analysis_status=analysis_status,
        from_date=from_date,
        to_date=to_date,
    )
    total_row = connection.execute(count_query, count_params).fetchone()
    total_filtered = total_row["total"] if total_row else 0

    # Hämta items
    query, params = _build_mfn_query(
        issuer_id=issuer_id,
        profile_category=profile_category,
        cash_relevance_min=cash_relevance_min,
        analysis_status=analysis_status,
        from_date=from_date,
        to_date=to_date,
        cursor=cursor,
        limit=limit,
    )

    rows = connection.execute(query, params).fetchall()

    # Kolla om det finns fler resultat
    has_more = len(rows) > limit
    items = rows[:limit]

    # Bygg next_cursor från sista item
    next_cursor = None
    if has_more and items:
        next_cursor = items[-1]["published_at"]

    return {
        "items": [dict(row) for row in items],
        "next_cursor": next_cursor,
        "total_filtered": total_filtered,
    }


def get_mfn_issuer_news(
    connection: sqlite3.Connection,
    issuer_id: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Hämta MFN-nyheter för ett specifikt bolag.

    Raises:
        ValueError: Om bolaget inte finns
    """
    # Verifiera att bolaget finns
    row = connection.execute(
        "SELECT id FROM issuers WHERE id = ?",
        (issuer_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Bolag med ID {issuer_id} hittades inte")

    return get_mfn_news(connection, issuer_id=issuer_id, **kwargs)
