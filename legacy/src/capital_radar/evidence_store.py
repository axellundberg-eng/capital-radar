"""Persistent full-text evidence with stable page and character positions."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from .normalization import utc_now


_PAGE_MARKER = re.compile(r"(?im)^\s*\[PAGE\s+(\d+)\]\s*$")

def source_text_available(text: str | None) -> bool:
    """Require content beyond page labels; this is not a completeness check.

    Inspect without changing archived text, hashes or Unicode offsets. Short
    real text is allowed; whitespace, punctuation and PDF labels alone are not.
    """
    return isinstance(text, str) and any(c.isalnum() for c in _PAGE_MARKER.sub('', text))

def _ensure_rehydration_table(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS document_evidence_rehydrations(
        document_id INTEGER PRIMARY KEY, source_url TEXT, status TEXT NOT NULL,
        idempotency_key TEXT, error TEXT, started_at TEXT, completed_at TEXT,
        updated_at TEXT
    )""")

def evidence_status(connection: sqlite3.Connection, document_id: int) -> dict[str, Any]:
    _ensure_rehydration_table(connection)
    evidence_id = ensure_document_evidence(connection, document_id)
    row = connection.execute("SELECT * FROM document_evidence_rehydrations WHERE document_id=?", (document_id,)).fetchone()
    latest = connection.execute('SELECT full_text FROM document_evidence_texts WHERE document_id=? ORDER BY id DESC LIMIT 1',
                                (document_id,)).fetchone()
    status = ("ready" if evidence_id is not None else "unusable_text" if latest is not None
              else str(row['status']) if row and row['status'] in {'rehydrating','failed'} else 'metadata_only')
    return {"evidence_status": status, "rehydration": dict(row) if row else None}


def _pages(text: str) -> list[tuple[int, int, int, str]]:
    matches = list(_PAGE_MARKER.finditer(text))
    if not matches:
        return [(1, 0, len(text), text)]
    result: list[tuple[int, int, int, str]] = []
    if matches[0].start() > 0 and text[:matches[0].start()].strip():
        result.append((1, 0, matches[0].start(), text[:matches[0].start()]))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((int(match.group(1)), start, end, text[start:end]))
    return result


def archive_document_evidence(
    connection: sqlite3.Connection, *, document_id: int, content_id: int | None,
    text: str | None, text_sha256: str | None = None, page_count: int | None = None,
) -> int | None:
    if not text:
        return None
    digest = text_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()
    connection.execute(
        """INSERT INTO document_evidence_texts(
               document_id,content_id,text_sha256,full_text,char_count,page_count,created_at
           ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(document_id,text_sha256) DO NOTHING""",
        (document_id, content_id, digest, text, len(text), page_count, utc_now()),
    )
    row = connection.execute(
        "SELECT id FROM document_evidence_texts WHERE document_id=? AND text_sha256=?",
        (document_id, digest),
    ).fetchone()
    evidence_id = int(row[0])
    connection.executemany(
        """INSERT OR IGNORE INTO document_evidence_pages(
               evidence_text_id,page_number,start_offset,end_offset,page_text
           ) VALUES(?,?,?,?,?)""",
        [(evidence_id, number, start, end, page_text)
         for number, start, end, page_text in _pages(text)],
    )
    # Preserve the extraction artifact, but never present labels as evidence.
    return evidence_id if source_text_available(text) else None


def ensure_document_evidence(connection: sqlite3.Connection, document_id: int) -> int | None:
    row = connection.execute(
        "SELECT id,full_text FROM document_evidence_texts WHERE document_id=? ORDER BY id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if row is not None:
        return int(row[0]) if source_text_available(row['full_text']) else None
    source = connection.execute(
        """SELECT id,extracted_text,text_sha256,page_count FROM document_contents
           WHERE document_id=? AND extracted_text IS NOT NULL
           ORDER BY CASE WHEN status='extracted' THEN 0 ELSE 1 END,id DESC LIMIT 1""",
        (document_id,),
    ).fetchone()
    if source is None:
        return None
    return archive_document_evidence(
        connection, document_id=document_id, content_id=int(source["id"]),
        text=str(source["extracted_text"]), text_sha256=source["text_sha256"],
        page_count=source["page_count"],
    )


def evidence_contains(connection: sqlite3.Connection, document_id: int, excerpt: str) -> bool:
    if not source_text_available(excerpt) or ensure_document_evidence(connection, document_id) is None:
        return False
    return connection.execute(
        """SELECT 1 FROM document_evidence_texts
           WHERE document_id=? AND instr(full_text,?)>0 LIMIT 1""",
        (document_id, excerpt),
    ).fetchone() is not None


def document_evidence(
    connection: sqlite3.Connection, document_id: int, *, page: int | None = None,
    start: int = 0, limit: int = 20000,
) -> dict[str, Any]:
    state = evidence_status(connection, document_id)
    evidence_id = ensure_document_evidence(connection, document_id)
    document = connection.execute(
        "SELECT id,issuer_id,title,canonical_url AS source_url,published_at FROM documents WHERE id=?",
        (document_id,),
    ).fetchone()
    if document is None:
        raise KeyError(document_id)
    if evidence_id is None:
        return {"document": dict(document), "available": False, "evidence_status": state["evidence_status"], "pages": [], "rehydration": state["rehydration"]}
    meta = connection.execute(
        "SELECT id,text_sha256,char_count,page_count,created_at FROM document_evidence_texts WHERE id=?",
        (evidence_id,),
    ).fetchone()
    if page is not None:
        rows = connection.execute(
            """SELECT page_number,start_offset,end_offset,page_text
               FROM document_evidence_pages WHERE evidence_text_id=? AND page_number=?""",
            (evidence_id, page),
        ).fetchall()
        pages = [dict(row) for row in rows]
        for item in pages:
            item["text"] = item.pop("page_text")
    else:
        start = max(0, int(start)); limit = min(100000, max(1, int(limit)))
        full = connection.execute(
            "SELECT substr(full_text,?,?) FROM document_evidence_texts WHERE id=?",
            (start + 1, limit, evidence_id),
        ).fetchone()[0]
        pages = [{"start_offset": start, "end_offset": start + len(full), "text": full}]
    return {"document": dict(document), "available": True, "evidence_status": "ready",
            "evidence": dict(meta), "pages": pages, "rehydration": state["rehydration"]}

def mark_rehydration(connection: sqlite3.Connection, document_id: int, *, status: str,
                     source_url: str | None = None, idempotency_key: str | None = None,
                     error: str | None = None) -> None:
    _ensure_rehydration_table(connection)
    now = utc_now()
    connection.execute("""INSERT INTO document_evidence_rehydrations
        (document_id,source_url,status,idempotency_key,error,started_at,completed_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
        source_url=excluded.source_url,status=excluded.status,idempotency_key=excluded.idempotency_key,
        error=excluded.error,completed_at=excluded.completed_at,updated_at=excluded.updated_at""",
        (document_id, source_url, status, idempotency_key, error,
         now if status == "rehydrating" else None,
         now if status in {"ready", "failed"} else None, now))
