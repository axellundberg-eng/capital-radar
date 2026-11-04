"""Complete, read-only transaction dossier for controlled agent operations."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .financing_corrections import effective_overrides
from .radar_analysis import current_analysis
from .transaction_feed import _transaction_items, get_transaction_detail

MAX_NEARBY_DOCUMENTS = 50
MAX_SOURCE_CHARS = 200_000
MAX_TOTAL_SOURCE_CHARS = 1_000_000
NEARBY_EXCERPT_CHARS = 1_200


def _rows(connection: sqlite3.Connection, sql: str, issuer_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, (issuer_id,))]


def _audit_rows(connection: sqlite3.Connection, table: str, issuer_id: int) -> list[dict[str, Any]]:
    # Table names come exclusively from the constants in get_transaction_case.
    result = _rows(connection, f"SELECT * FROM {table} WHERE issuer_id=? ORDER BY id", issuer_id)
    for row in result:
        for key, value in tuple(row.items()):
            if key.endswith("_json") and value is not None:
                row[key.removesuffix("_json")] = json.loads(value)
    return result


def get_transaction_case(connection: sqlite3.Connection, transaction_id: str) -> dict[str, Any]:
    """Read cached source text, originals, effective values and audits; never fetch or interpret."""
    return _build_transaction_case(connection, transaction_id)


def _build_transaction_case(connection: sqlite3.Connection, transaction_id: str) -> dict[str, Any]:
    transaction = get_transaction_detail(connection, transaction_id)
    issuer_id = int(transaction["issuer_id"])
    documents = _rows(connection, """SELECT id,issuer_id,title,canonical_url,
        published_at,language,analysis_status FROM documents WHERE issuer_id=? ORDER BY published_at,id""", issuer_id)
    selected_ids = set(transaction.get("source_document_ids", []))
    selected = [document for document in documents if document["id"] in selected_ids]
    nearby = [document for document in reversed(documents) if document["id"] not in selected_ids]
    omitted_nearby = max(0, len(nearby) - MAX_NEARBY_DOCUMENTS)
    documents = selected + nearby[:MAX_NEARBY_DOCUMENTS]
    by_document: dict[int, list[dict[str, Any]]] = {}
    remaining = MAX_TOTAL_SOURCE_CHARS
    for document in documents:
        limit = min(remaining, MAX_SOURCE_CHARS if document["id"] in selected_ids else NEARBY_EXCERPT_CHARS)
        contents = [dict(row) for row in connection.execute("""SELECT document_id,source_url,
            substr(extracted_text,1,?) AS extracted_text,length(extracted_text) AS source_char_count,status
            FROM document_contents WHERE document_id=? ORDER BY id""", (limit, document["id"]))]
        if not any(content.get("source_char_count") for content in contents):
            archived = connection.execute(
                """SELECT document_id,'evidence_archive' AS source_url,
                   substr(full_text,1,?) AS extracted_text,char_count AS source_char_count,
                   'archived_evidence' AS status
                   FROM document_evidence_texts WHERE document_id=? ORDER BY id DESC LIMIT 1""",
                (limit, document["id"]),
            ).fetchone()
            if archived is not None:
                contents = [dict(archived)]
        for content in contents:
            text = str(content["extracted_text"] or "")[:remaining]
            content["extracted_text"] = text or None
            content["omitted_char_count"] = max(0, int(content["source_char_count"] or 0) - len(text))
            remaining -= len(text)
        by_document[int(document["id"])] = contents
    for document in documents:
        document["in_selected_transaction"] = document["id"] in selected_ids
        document["contents"] = by_document.get(int(document["id"]), [])
        document["text_available"] = any(item.get("source_char_count") for item in document["contents"])
    events = _rows(connection, "SELECT * FROM financing_events WHERE issuer_id=? ORDER BY id", issuer_id)
    patches = effective_overrides(connection, [int(event["id"]) for event in events])
    evidence = _rows(connection, """SELECT evidence.* FROM financing_event_evidence evidence
        JOIN financing_events event ON event.id=evidence.financing_event_id
        WHERE event.issuer_id=? ORDER BY evidence.id""", issuer_id)
    audit = {
        name: _audit_rows(connection, table, issuer_id)
        for name, table in (
            ("financing_corrections", "financing_correction_proposals"),
            ("grouping_corrections", "financing_transaction_corrections"),
            ("value_corrections", "financing_transaction_value_corrections"),
            ("document_links", "financing_transaction_document_links"),
            ("document_round_overrides", "financing_transaction_document_overrides"),
            ("field_proposals", "financing_field_proposals"),
            ("round_memberships", "financing_round_memberships"),
            ("adjusted_runway_recalculations", "adjusted_runway_recalculations"),
        )
    }
    return {
        "transaction": {**transaction, "transaction_id": transaction["id"]},
        "issuer_id": issuer_id,
        "issuer_transactions": [{**item, "transaction_id": item["id"]} for item in _transaction_items(connection) if int(item["issuer_id"]) == issuer_id],
        "documents": documents,
        "financing_events": [
            {"financing_event_id": int(event["id"]), "original_values": event,
             "effective_values": {**event, **patches.get(int(event["id"]), {})}}
            for event in events
        ],
        "financing_event_evidence": evidence,
        "audit": audit,
        "analysis": next(iter(current_analysis(connection, [issuer_id])), None),
        "coverage": {"selected_document_count": len(selected), "nearby_documents_omitted": omitted_nearby,
                     "nearby_excerpt_chars": NEARBY_EXCERPT_CHARS, "max_source_chars": MAX_SOURCE_CHARS,
                     "max_total_source_chars": MAX_TOTAL_SOURCE_CHARS,
                     "omitted_source_chars": sum(item["omitted_char_count"] for values in by_document.values() for item in values)},
        "source_policy": {"raw_extraction_immutable": True, "source_documents_immutable": True,
                          "reported_runway_immutable": True, "stored_text_only": True},
    }
