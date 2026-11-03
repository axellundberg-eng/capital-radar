"""Explicit MFN identities, guarded by the existing listed security.

Evidence: https://www.mfn.se/all/a/aira (checked 2026-10-21).
Do not infer issuer equivalence from a ticker or a city suffix alone.
"""
from __future__ import annotations

import sqlite3

from .normalization import normalize_issuer_name


_AIRA_UNIVERSE_NAME = "ai revenue assistant software"
_AIRA_SOURCE_NAME = "ai revenue assistant software stockholm"


def _listed_aira_id(connection: sqlite3.Connection) -> int | None:
    rows = connection.execute(
        """
        SELECT DISTINCT i.id FROM issuers i
        JOIN securities s ON s.issuer_id=i.id
        WHERE i.normalized_name=? AND s.active=1
          AND UPPER(TRIM(s.symbol))='AIRA'
          AND s.source_id='nasdaq_nordic'
          AND s.market='Nasdaq First North Growth Market Sweden'
        LIMIT 2
        """,
        (_AIRA_UNIVERSE_NAME,),
    ).fetchall()
    return int(rows[0]["id"]) if len(rows) == 1 else None


def known_mfn_issuer(
    connection: sqlite3.Connection, source_name: str
) -> int | None:
    if normalize_issuer_name(source_name) != _AIRA_SOURCE_NAME:
        return None
    return _listed_aira_id(connection)


def known_mfn_slug(connection: sqlite3.Connection, issuer_id: int) -> str | None:
    return "aira" if _listed_aira_id(connection) == issuer_id else None
