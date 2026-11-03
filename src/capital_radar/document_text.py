"""Hämta och spara fulltext för ett enskilt dokument vid behov.

Utbruten oförändrad ur NewsRepository.ensure_article_text i
Screener-analys-hus (news_commentary följer inte med till Capital Radar).
"""

from __future__ import annotations

from typing import Any

from .db import Database
from .documents import SafeDocumentFetcher, extract_document
from .normalization import stable_hash, utc_now


def ensure_document_text(db: Database, document_id: int, settings: Any) -> str:
    with db.connect() as c:
        existing = c.execute(
            "SELECT extracted_text FROM document_contents WHERE document_id=? AND status='extracted' "
            "AND extracted_text IS NOT NULL ORDER BY char_count DESC,id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        if existing:
            return str(existing[0])
        document = c.execute(
            "SELECT id, canonical_url FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    if document is None:
        raise KeyError(document_id)
    fetcher = SafeDocumentFetcher(
        allowed_hosts=settings.document_allowed_hosts,
        user_agent=settings.user_agent,
        timeout=min(settings.http_timeout, 45.0),
        max_bytes=settings.document_max_bytes,
    )
    downloaded = fetcher.get(str(document["canonical_url"]))
    extracted = extract_document(downloaded.body, downloaded.mime_type, downloaded.final_url)
    digest = stable_hash(downloaded.body)
    text_hash = stable_hash(extracted.text)
    now = utc_now()
    with db.connect() as c:
        c.execute(
            """INSERT INTO document_contents(document_id,source_url,final_url,local_path,mime_type,sha256,byte_size,extraction_method,extracted_text,text_sha256,char_count,page_count,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id,source_url) DO UPDATE SET final_url=excluded.final_url,local_path=excluded.local_path,mime_type=excluded.mime_type,sha256=excluded.sha256,byte_size=excluded.byte_size,extraction_method=excluded.extraction_method,extracted_text=excluded.extracted_text,text_sha256=excluded.text_sha256,char_count=excluded.char_count,page_count=excluded.page_count,status=excluded.status,updated_at=excluded.updated_at""",
            (document_id, document["canonical_url"], downloaded.final_url, None, downloaded.mime_type, digest, len(downloaded.body), extracted.method, extracted.text, text_hash, len(extracted.text), extracted.page_count, extracted.status, now, now),
        )
        c.execute("UPDATE documents SET content_sha256=?,updated_at=? WHERE id=?", (text_hash, now, document_id))
    return extracted.text
