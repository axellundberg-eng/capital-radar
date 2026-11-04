"""Läser granskarens indata. Bara SELECT — inga skrivningar i DeepSeeks data.

Granskaren får källcitaten och de sidor citaten pekar på, inte hela
dokumentet. Sidtexten hämtas ur document_evidence_pages (som överlever
purge av råtext efter extraction) med document_contents som reserv.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from ..evidence_store import _pages

_WS = re.compile(r"\s+")

# Kolumner som granskas. Övriga (garantitrancher, marknadsdata) hör till
# andra flöden och skulle bara kosta tokens här.
_FACT_COLUMNS = (
    "id", "fact_type", "reported_value", "normalized_value", "currency",
    "reported_unit", "period_start", "period_end", "as_of_date",
    "evidence_label", "source_page", "source_excerpt",
)
_EVENT_COLUMNS = (
    "event_type", "description", "announcement_date", "effective_date",
    "status", "cash_direction", "gross_amount", "net_amount",
    "cash_effect_amount", "currency", "liquidity_effect_kind",
    "committed_amount", "conditional_amount", "reflected_in_report",
    "credit_limit", "drawn_amount", "undrawn_commitment", "conditions",
)


def normalize_for_match(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


@dataclass
class ExtractionInput:
    extraction_job_id: int
    issuer_id: int
    document_id: int
    title: str
    published_at: str
    context: dict[str, Any]
    facts: list[dict[str, Any]]
    events: list[dict[str, Any]]
    pages: list[tuple[int, str]] = field(default_factory=list)
    source_status: str = "missing"

    @property
    def targets(self) -> set[tuple[str, int]]:
        return {("financial_fact", int(f["fact_id"])) for f in self.facts} | {
            ("financing_event", int(e["event_id"])) for e in self.events
        }

    @property
    def quote_haystack(self) -> str:
        parts = [text for _, text in self.pages]
        parts += [str(f.get("source_excerpt") or "") for f in self.facts]
        parts += [str(e.get("source_excerpt") or "") for e in self.events]
        return normalize_for_match("\n".join(parts))


def list_reviewable_jobs(
    connection: sqlite3.Connection,
    *,
    task: str,
    model: str,
    prompt_version: str,
    limit: int,
    retry_failed: bool,
    now: str,
) -> list[int]:
    """De N senaste färdiga extraction-jobben utan granskning."""
    blocking = (
        "r.status IN ('completed','incomplete','validation_failed','refused') "
        "OR (r.status='running' AND (r.deadline_at IS NULL OR r.deadline_at > :now))"
        if retry_failed
        else "1=1"
    )
    rows = connection.execute(
        f"""
        SELECT j.id FROM extraction_jobs j
        WHERE j.status='completed' AND j.parsed_output_json IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM review_runs r
              WHERE r.task=:task AND r.extraction_job_id=j.id
                AND r.model=:model AND r.prompt_version=:prompt_version
                AND ({blocking})
          )
        ORDER BY j.id DESC
        LIMIT :limit
        """,
        {
            "task": task, "model": model, "prompt_version": prompt_version,
            "limit": limit, "now": now,
        },
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _context_from_parsed(parsed_json: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(parsed_json or "{}")
    except json.JSONDecodeError:
        return {"parse_error": True}
    report = parsed.get("report") or {}
    quality = parsed.get("quality") or {}
    return {
        "document_class": parsed.get("document_class"),
        "document_summary": parsed.get("document_summary"),
        "report": {
            key: report.get(key)
            for key in (
                "is_financial_report", "period_start", "period_end",
                "publication_date", "currency", "reported_scale",
                "period_months",
            )
        },
        "extractor_uncertainties": quality.get("uncertainties") or [],
    }


def _all_pages(
    connection: sqlite3.Connection, document_id: int
) -> tuple[list[tuple[int, str]], str]:
    evidence = connection.execute(
        "SELECT id FROM document_evidence_texts WHERE document_id=? "
        "ORDER BY id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if evidence is not None:
        rows = connection.execute(
            "SELECT page_number, page_text FROM document_evidence_pages "
            "WHERE evidence_text_id=? ORDER BY page_number",
            (int(evidence["id"]),),
        ).fetchall()
        if rows:
            return [(int(r["page_number"]), str(r["page_text"])) for r in rows], "evidence"
    content = connection.execute(
        "SELECT extracted_text FROM document_contents "
        "WHERE document_id=? AND extracted_text IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if content is not None and content["extracted_text"]:
        return [(n, t) for n, _s, _e, t in _pages(str(content["extracted_text"]))], "content"
    return [], "missing"


def _select_pages(
    pages: list[tuple[int, str]],
    excerpts: list[tuple[Optional[int], str]],
    max_chars: int,
) -> list[tuple[int, str]]:
    """Sidor som citaten pekar på (sidnummer eller textträff), med teckentak."""
    wanted: set[int] = set()
    normalized = [(n, normalize_for_match(t)) for n, t in pages]
    known = {n for n, _ in pages}
    for page, excerpt in excerpts:
        if page is not None and int(page) in known:
            wanted.add(int(page))
        needle = normalize_for_match(excerpt)[:80]
        if len(needle) >= 12:
            wanted.update(n for n, text in normalized if needle in text)
    selected: list[tuple[int, str]] = []
    used = 0
    for number, text in pages:
        if number not in wanted:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunk = text if len(text) <= remaining else text[:remaining] + "\n[…avkortad]"
        selected.append((number, chunk))
        used += len(chunk)
    return selected


def load_extraction_input(
    connection: sqlite3.Connection, job_id: int, *, max_source_chars: int
) -> ExtractionInput | None:
    job = connection.execute(
        """
        SELECT j.id, j.issuer_id, j.document_id, j.parsed_output_json,
               d.title, d.published_at
        FROM extraction_jobs j JOIN documents d ON d.id=j.document_id
        WHERE j.id=? AND j.status='completed'
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        return None
    facts = [
        {("fact_id" if k == "id" else k): row[k] for k in _FACT_COLUMNS}
        for row in connection.execute(
            f"SELECT {', '.join(_FACT_COLUMNS)} FROM financial_facts "
            "WHERE extraction_job_id=? ORDER BY id",
            (job_id,),
        )
    ]
    event_select = ", ".join(f"e.{c}" for c in _EVENT_COLUMNS)
    events = [
        {"event_id": row["id"], **{k: row[k] for k in _EVENT_COLUMNS},
         "source_page": row["source_page"], "source_excerpt": row["source_excerpt"]}
        for row in connection.execute(
            f"""
            SELECT e.id, {event_select}, ev.source_page, ev.source_excerpt
            FROM financing_event_evidence ev
            JOIN financing_events e ON e.id=ev.financing_event_id
            WHERE ev.extraction_job_id=? ORDER BY e.id
            """,
            (job_id,),
        )
    ]
    pages, status = _all_pages(connection, int(job["document_id"]))
    excerpts = [(f["source_page"], str(f["source_excerpt"] or "")) for f in facts]
    excerpts += [(e["source_page"], str(e["source_excerpt"] or "")) for e in events]
    selected = _select_pages(pages, excerpts, max_source_chars)
    return ExtractionInput(
        extraction_job_id=int(job["id"]),
        issuer_id=int(job["issuer_id"]),
        document_id=int(job["document_id"]),
        title=str(job["title"]),
        published_at=str(job["published_at"]),
        context=_context_from_parsed(job["parsed_output_json"]),
        facts=facts,
        events=events,
        pages=selected,
        source_status=status if selected else ("no_cited_pages" if pages else status),
    )


def render_extraction_prompt(item: ExtractionInput) -> str:
    payload = {
        "document": {
            "document_id": item.document_id,
            "title": item.title,
            "published_at": item.published_at,
        },
        "extraction_context": item.context,
        "amount_convention": (
            "financing_event-belopp är i kronor (SEK). financial_fact: "
            "reported_value som i källan, normalized_value i kronor."
        ),
        "financial_facts": item.facts,
        "financing_events": item.events,
        "source_text_status": item.source_status,
    }
    pages = "\n\n".join(f"[SIDA {n}]\n{text.strip()}" for n, text in item.pages)
    return (
        "Granska raderna nedan mot källtexten.\n\n"
        f"<extraction>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</extraction>\n\n"
        f"<source_pages>\n{pages or '(ingen sidtext tillgänglig)'}\n</source_pages>"
    )


@dataclass
class HeadlineCandidate:
    document_id: int
    issuer_id: int
    title: str
    published_at: str
    category: Optional[str]
    cash_relevance_score: int
    relevance_reason: str


def list_filtered_headlines(
    connection: sqlite3.Connection,
    *,
    task: str,
    model: str,
    prompt_version: str,
    limit: int,
    max_score: int = 55,
) -> list[HeadlineCandidate]:
    """Dokument som rubrikfiltret inte släppte vidare och som inte granskats."""
    rows = connection.execute(
        """
        SELECT d.id, d.issuer_id, d.title, d.published_at, d.category,
               d.cash_relevance_score, d.relevance_reason
        FROM documents d
        WHERE d.queued_for_analysis=0 AND d.cash_relevance_score < :max_score
          AND NOT EXISTS (
              SELECT 1 FROM review_verdicts v
              WHERE v.task=:task AND v.target_kind='document'
                AND v.target_id=d.id AND v.model=:model
                AND v.prompt_version=:prompt_version
          )
        ORDER BY d.published_at DESC, d.id DESC
        LIMIT :limit
        """,
        {
            "task": task, "model": model, "prompt_version": prompt_version,
            "limit": limit, "max_score": max_score,
        },
    ).fetchall()
    return [
        HeadlineCandidate(
            document_id=int(r["id"]),
            issuer_id=int(r["issuer_id"]),
            title=str(r["title"]),
            published_at=str(r["published_at"]),
            category=r["category"],
            cash_relevance_score=int(r["cash_relevance_score"]),
            relevance_reason=str(r["relevance_reason"]),
        )
        for r in rows
    ]


def render_headline_prompt(batch: list[HeadlineCandidate]) -> str:
    payload = [
        {
            "document_id": c.document_id,
            "title": c.title,
            "published_at": c.published_at[:10],
            "category": c.category,
            "filter_reason": c.relevance_reason,
        }
        for c in batch
    ]
    return (
        "Rubriker som filtret sorterade bort:\n\n"
        f"<headlines>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</headlines>"
    )
