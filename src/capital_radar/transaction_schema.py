"""Shared public contract for the read-only transaction overview."""

from __future__ import annotations

from typing import Any


def read_model_schema() -> dict[str, Any]:
    """Freshness is explicit; a presentation snapshot is not strict readback."""
    return {"type": "object", "required": [
        "status", "stale", "refreshing", "generated_at", "generation", "retry_after",
    ], "properties": {
        "status": {"type": "string", "enum": ["pending", "refreshing", "stale", "ready", "failed"]},
        "stale": {"type": "boolean"}, "refreshing": {"type": "boolean"},
        "generated_at": {"type": "string", "format": "date-time", "nullable": True},
        "generation": {"type": "string", "nullable": True},
        "retry_after": {"type": "integer", "minimum": 1},
        "error_code": {"type": "string"},
    }}


def read_model_pending_response() -> dict[str, Any]:
    return {"description": (
        "The durable presentation read model is warming. No completed empty result "
        "is implied. Retry after read_model.retry_after seconds. Strict correction "
        "validation and audit readback never use this presentation snapshot."
    ), "content": {"application/json": {"schema": {
        "type": "object", "required": ["read_model"],
        "properties": {"read_model": read_model_schema()},
    }}}}


def transaction_feed_operation() -> dict[str, Any]:
    text = {"type": "string"}
    nullable_text = {"type": "string", "nullable": True}
    source = {
        "type": "object",
        "required": ["document_id", "published_at", "title", "url", "status"],
        "properties": {
            "document_id": {"type": "integer"}, "published_at": text,
            "title": text, "url": text, "status": text,
        },
    }
    item_fields = {
        "id": text, "issuer_id": {"type": "integer"}, "issuer_name": text,
        "category": text, "category_label": text, "status": text,
        "status_label": text,
        "signal_tone": {"type": "string", "enum": ["green", "yellow", "red", "neutral"]},
        "signal_label": text, "headline": text, "summary": text,
        "editorial_source": {"type": "string", "enum": ["source", "ai"]},
        "amount": {"type": "number", "nullable": True},
        "currency": nullable_text, "amount_basis": nullable_text,
        "institutions": {"type": "array", "items": {
            "type": "object", "required": ["name", "role"],
            "properties": {"name": text, "role": text},
        }},
        "published_at": text, "source_url": text,
        "source_document_ids": {"type": "array", "items": {"type": "integer"}},
        "history": {"type": "array", "items": source},
        "details": {
            "type": "object",
            "required": [
                "overview", "announced_amount", "announced_amount_basis",
                "outcome_amount", "outcome_amount_basis", "currency",
                "completion_ratio_pct", "source_count", "lifecycle",
                "detail_summary", "key_points", "terms", "use_of_proceeds",
                "next_milestone",
            ],
            "properties": {
                "overview": text,
                "announced_amount": {"type": "number", "nullable": True},
                "announced_amount_basis": nullable_text,
                "outcome_amount": {"type": "number", "nullable": True},
                "outcome_amount_basis": nullable_text,
                "currency": nullable_text,
                "completion_ratio_pct": {"type": "number", "nullable": True},
                "source_count": {"type": "integer"},
                "lifecycle": {"type": "array", "items": {
                    "type": "object",
                    "required": ["document_id", "published_at", "title", "url", "status", "status_label"],
                    "properties": {**source["properties"], "status_label": text},
                }},
                "detail_summary": nullable_text,
                "key_points": {"type": "array", "items": text},
                "terms": {"type": "array", "items": text},
                "use_of_proceeds": nullable_text,
                "next_milestone": nullable_text,
            },
        },
        "review_required": {"type": "boolean"},
    }
    fields = {
        "items": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "required": list(item_fields), "properties": item_fields,
        }},
        "page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "enum": [10]},
        "total": {"type": "integer", "minimum": 0},
        "total_pages": {"type": "integer", "minimum": 0},
        "has_next": {"type": "boolean"}, "as_of": nullable_text,
    }
    return {"get": {
        "operationId": "getTransactions",
        "summary": "Senaste finansieringstransaktionerna och deras utfall",
        "description": (
            "Tio finansieringstransaktioner per sida, sorterade efter senaste "
            "publicerade källa. Kopplade annonseringar och utfall visas som en "
            "uppdaterad transaktion med bevarad källhistorik. Alla belopp avser "
            "angiven amount_basis och valuta. Saknade belopp är null. Endast "
            "lokalt MFN-underlag och tidigare validerad AI-text läses; anropet "
            "startar ingen synk, analys eller AI-generering. En beständig "
            "läsmodell uppdateras i bakgrunden på samma diskvolym. read_model "
            "anger om snapshoten är aktuell eller äldre. Kallstart ger 202 "
            "och retry_after; det betyder inte att affärerna saknas."
        ),
        "tags": ["MFN"],
        "parameters": [
            {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100_000, "default": 1}},
            {"name": "page_size", "in": "query", "schema": {"type": "integer", "enum": [10], "default": 10}},
            *[{"name": name, "in": "query", "schema": {"type": "string", "maxLength": 256}, "description": description}
              for name, description in {
                  "q": "Case-insensitive text in issuer name, headline or summary; applied before pagination.",
                  "category": "Canonical financing category.",
                  "stage": "Canonical status or announcement (announced/proposed/conditional), terms (subscription_period/approved), outcome (preliminary_outcome/completed), completed (registered only). Canonical completed means outcome, not settlement.",
                  "advisor": "Case-insensitive financial advisor name substring.",
                  "agent": "Case-insensitive issuing agent name substring.",
                  "from": "Inclusive publication date, YYYY-MM-DD.",
              }.items()],
        ],
        "responses": {
            "200": {"description": "Transaktioner med aktuell källstatus", "content": {
                "application/json": {"schema": {
                    "type": "object", "required": list(fields),
                    "properties": {**fields, "read_model": read_model_schema()},
                }},
            }},
            "202": read_model_pending_response(),
            "400": {"description": "Ogiltig sidindelning"},
            "503": {"description": "Underlaget är tillfälligt otillgängligt"},
        },
    }}


def transaction_detail_operation() -> dict[str, Any]:
    """Document the canonical transaction detail read."""
    feed_schema = transaction_feed_operation()["get"]["responses"]["200"]
    item_schema = feed_schema["content"]["application/json"]["schema"]
    item_schema = item_schema["properties"]["items"]["items"]
    item_schema = {**item_schema, "properties": {
        **item_schema["properties"], "read_model": read_model_schema(),
    }}
    return {"get": {
        "operationId": "getTransactionDetail",
        "summary": "Sammanhängande transaktionsdetalj och källhistorik",
        "description": (
            "Returnerar en affär med aktuellt stadium, annonserat belopp, utfall, "
            "AI-bearbetad fördjupning och hela den länkade källhistoriken. Läsningen "
            "startar ingen AI-körning och skriver inte till databasen."
        ),
        "tags": ["MFN"],
        "parameters": [{
            "name": "transaction_id", "in": "path", "required": True,
            "schema": {"type": "string"},
        }],
        "responses": {
            "200": {"description": "Aktuell transaktionsdetalj", "content": {
                "application/json": {"schema": item_schema},
            }},
            "202": read_model_pending_response(),
            "400": {"description": "Ogiltigt transaktions-ID"},
            "404": {"description": "Transaktionen hittades inte"},
            "503": {"description": "Underlaget är tillfälligt otillgängligt"},
        },
    }}
