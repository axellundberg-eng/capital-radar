"""Största aktieägare ur rapporttext, tolkade med DeepSeek (JSON).

För varje bolag väljs den nyaste rapporten med en sida som ser ut att innehålla en ägartabell. Bara dessa sidor
(högst två, högst 6000 tecken var) skickas. Värden som texten inte anger blir null; inget gissas. Har bolagets
nyaste matchande rapport redan bearbetats hoppas bolaget över, och saknas matchande sida noteras 'no_table'
utan att några tokens används.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Callable

from .extract import ChatClient, ExtractionError, json_object, parse_date, parse_number
from .settings import Settings

PAGE_PATTERN = re.compile(
    r"största aktieägare|största ägare|aktieägare per|ägarförteckning|largest shareholders|major shareholders",
    re.IGNORECASE)
MAX_PAGES = 2
MAX_PAGE_CHARS = 6000
MAX_ROWS = 15

PROMPT = """Du läser sidor ur en rapport från ett börsnoterat bolag och ska hämta tabellen över bolagets
största aktieägare.

Svara med enbart ett JSON-objekt:
{
  "as_of": "YYYY-MM-DD" eller null,
  "rows": [
    {"name": "ägarens namn", "shares": antal aktier som tal eller null,
     "pct_capital": procent av kapitalet som tal eller null, "pct_votes": procent av rösterna som tal eller null}
  ]
}

Regler:
- as_of är det datum ägarlistan avser (t.ex. "per 30 september 2026" ger 2026-09-30); null om det inte står.
- Högst 15 rader, i den ordning tabellen har dem (största först). Ta bara med riktiga ägare, inte summarader
  som "Övriga" eller "Totalt".
- shares är exakt antal aktier (utan mellanrum). Procent anges som tal, t.ex. 12.4 (inte 0.124).
- Ange null för varje värde som inte uttryckligen står i texten. Räkna aldrig om och gissa aldrig.
- Finns ingen ägartabell i texten: {"as_of": null, "rows": []}.
"""


_DAY: str | None = None   # dagen som körningen gäller; tidsstämplar får samma dag som kontrollen jämför med


def _now() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return f"{_DAY}{stamp[10:]}" if _DAY else stamp


def _percent(value) -> float | None:
    number = parse_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def parse_owners(raw: str) -> tuple[str | None, list[dict]]:
    """(as_of, rader) ur modellens svar; trasigt svar ger ExtractionError."""
    data = json_object(raw)
    try:
        as_of = parse_date(data.get("as_of"))
    except ExtractionError:
        as_of = None
    rows = []
    for item in data.get("rows") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:200]
        if not name:
            continue
        shares = parse_number(item.get("shares"))
        rows.append({"name": name, "shares": shares if shares is not None and shares >= 0 else None,
                     "pct_capital": _percent(item.get("pct_capital")), "pct_votes": _percent(item.get("pct_votes"))})
        if len(rows) >= MAX_ROWS:
            break
    return as_of, rows


def matching_document(connection: sqlite3.Connection, company_id: int) -> tuple[int, list[str]] | None:
    """(dokument-id, sidtexter) för bolagets nyaste rapport med matchande sidor, annars None."""
    reports = connection.execute(
        """SELECT document_id FROM reports WHERE company_id=? AND EXISTS
           (SELECT 1 FROM report_pages p WHERE p.document_id = reports.document_id)
           ORDER BY published_at DESC, document_id DESC""", (company_id,)).fetchall()
    for report in reports:
        pages = [r["text"] for r in connection.execute(
            "SELECT text FROM report_pages WHERE document_id=? ORDER BY page", (report["document_id"],))
            if PAGE_PATTERN.search(r["text"])]
        if pages:
            return report["document_id"], pages[:MAX_PAGES]
    return None


def build_message(pages: list[str]) -> str:
    return "Rapportens sidor:\n\n" + "\n\n".join(
        f"--- Sida {n} ---\n{text[:MAX_PAGE_CHARS]}" for n, text in enumerate(pages, 1))


def ask(client: ChatClient, message: str, *, attempts: int = 2) -> tuple[str | None, list[dict], int]:
    """(as_of, rader, tokens). Ett nytt försök vid trasigt svar, sedan fel."""
    tokens = 0
    last: ExtractionError | None = None
    for _ in range(max(1, attempts)):
        try:
            raw, usage = client.complete(PROMPT, message)
            tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            as_of, rows = parse_owners(raw)
            return as_of, rows, tokens
        except ExtractionError as error:
            last = error
    error = ExtractionError(str(last))
    error.tokens = tokens  # type: ignore[attr-defined]
    raise error


def _record(connection: sqlite3.Connection, company_id: int, document_id: int | None, status: str,
            error: str | None, tokens: int) -> None:
    connection.execute(
        """INSERT INTO owner_fetch(company_id, document_id, status, error, tokens, fetched_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(company_id) DO UPDATE SET document_id=excluded.document_id, status=excluded.status,
           error=excluded.error, tokens=excluded.tokens, fetched_at=excluded.fetched_at""",
        (company_id, document_id, status, error, tokens, _now()))


def store(connection: sqlite3.Connection, company_id: int, document_id: int, as_of: str | None,
          rows: list[dict], tokens: int) -> None:
    """Ersätt bolagets ägarlista med den nya (eller notera tom lista som no_table)."""
    with connection:
        connection.execute("DELETE FROM shareholders WHERE company_id=?", (company_id,))
        now = _now()
        connection.executemany(
            """INSERT INTO shareholders(company_id, document_id, as_of, rank, name, shares, pct_capital, pct_votes,
               created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(company_id, document_id, as_of, rank, r["name"], r["shares"], r["pct_capital"], r["pct_votes"], now)
             for rank, r in enumerate(rows, 1)])
        _record(connection, company_id, document_id, "ok" if rows else "no_table", None, tokens)


def candidates(connection: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """Bolag med rapporttext vars nyaste matchande dokument inte redan är bearbetat; prospekt (runway) först."""
    return connection.execute(
        """SELECT c.id, c.name, EXISTS(SELECT 1 FROM reports r JOIN runway_results x ON x.document_id = r.document_id
                                       WHERE r.company_id = c.id) AS has_runway
           FROM companies c WHERE EXISTS (SELECT 1 FROM reports r WHERE r.company_id = c.id AND EXISTS
                                          (SELECT 1 FROM report_pages p WHERE p.document_id = r.document_id))
           ORDER BY has_runway DESC, c.id""").fetchall()


def run(connection: sqlite3.Connection, settings: Settings | None, client: ChatClient, *, limit: int = 30,
        token_budget: int = 60000, log: Callable[[str], None] = print, today: str | None = None) -> dict[str, int]:
    """Tolka ägartabeller för högst `limit` bolag inom tokenbudgeten. Returnerar räknare."""
    counts = {"processed": 0, "ok": 0, "no_table": 0, "failed": 0, "skipped": 0, "tokens": 0, "remaining": 0}
    today = today or date.today().isoformat()
    global _DAY
    _DAY = today
    for company in candidates(connection, today):
        found = matching_document(connection, company["id"])
        document_id = found[0] if found else None
        previous = connection.execute("SELECT * FROM owner_fetch WHERE company_id=?", (company["id"],)).fetchone()
        if previous and previous["document_id"] == document_id and (
                previous["status"] != "failed" or previous["fetched_at"].startswith(today)):
            counts["skipped"] += 1
            continue
        if found is None:
            with connection:
                _record(connection, company["id"], None, "no_table", None, 0)   # ingen tokenkostnad
            counts["no_table"] += 1
            continue
        if counts["processed"] >= limit or counts["tokens"] >= token_budget:
            counts["remaining"] += 1
            continue
        counts["processed"] += 1
        try:
            as_of, rows, tokens = ask(client, build_message(found[1]))
        except ExtractionError as error:
            tokens = getattr(error, "tokens", 0)
            counts["tokens"] += tokens
            counts["failed"] += 1
            with connection:
                _record(connection, company["id"], document_id, "failed", str(error)[:300], tokens)
            continue
        counts["tokens"] += tokens
        store(connection, company["id"], document_id, as_of, rows, tokens)
        counts["ok" if rows else "no_table"] += 1
    log(f"ägare: {counts}")
    return counts


def latest(connection: sqlite3.Connection, company_id: int) -> dict | None:
    """Bolagets senaste ägarlista i API-formen, eller None."""
    rows = connection.execute(
        """SELECT s.*, r.title FROM shareholders s LEFT JOIN reports r ON r.document_id = s.document_id
           WHERE s.company_id=? ORDER BY s.rank""", (company_id,)).fetchall()
    if not rows:
        return None
    top = [{"name": r["name"], "shares": r["shares"], "pct_capital": r["pct_capital"], "pct_votes": r["pct_votes"]}
           for r in rows]
    pcts = sorted((r["pct_capital"] for r in rows if r["pct_capital"] is not None), reverse=True)

    def share_of(count: int) -> float | None:
        return round(sum(pcts[:count]), 1) if pcts else None

    large = sum(p for p in pcts if p >= 5.0)
    return {"as_of": rows[0]["as_of"], "source_document_id": rows[0]["document_id"], "source_title": rows[0]["title"],
            "top": top, "top3_pct": share_of(3), "top10_pct": share_of(10),
            "free_float_estimate_pct": round(max(0.0, 100.0 - large), 1) if pcts else None}
