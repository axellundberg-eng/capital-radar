"""Steg 2: kör kedjan för senaste rapporten per bolag, kostnadsmedvetet.

- Senaste rapporten med PDF per bolag. Är den bara ett kort pressmeddelande utan
  balansräkning (vanligt för kommunikéer och kallelser) prövas nästa äldre
  rapport, högst MAX_FALLBACKS gånger. Kontrollen sker före AI-anropet.
- Redan extraherade rapporter hoppas över (inga nya AI-anrop).
- Flera arbetare parallellt, var och en med egen databasanslutning.
- Tokenbudget: inga nya extraktioner startas när budgeten är förbrukad.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .db import connect
from .extract import ChatClient
from .pipeline import process
from .settings import Settings

# En rapport per bolag: senaste publiceringen; vid samma tidpunkt (svensk och engelsk
# version) den med lägst id.
LATEST_REPORTS = """
SELECT r.document_id, c.name FROM reports r JOIN companies c ON c.id = r.company_id
WHERE r.document_id = (
    SELECT r2.document_id FROM reports r2 WHERE r2.company_id = r.company_id AND r2.pdf_url IS NOT NULL
    ORDER BY r2.published_at DESC, r2.document_id ASC LIMIT 1)
ORDER BY c.name COLLATE NOCASE
"""


MAX_FALLBACKS = 2
SHORT_PAGES = 4
BALANCE_WORDS = re.compile(
    r"summa tillgångar|summa eget kapital|total assets|balansräkning|balance sheet|"
    r"likvida medel\s*[-\d]|cash and cash equivalents\s*[-\d]", re.I)


def company_reports(connection: sqlite3.Connection, document_id: int) -> list[int]:
    """Bolagets rapporter med PDF, nyast först, med start i den angivna."""
    company = connection.execute("SELECT company_id, published_at FROM reports WHERE document_id=?",
                                 (document_id,)).fetchone()
    rows = connection.execute(
        """SELECT document_id FROM reports WHERE company_id=? AND pdf_url IS NOT NULL
           AND (published_at < ? OR document_id = ?) ORDER BY published_at DESC, document_id ASC""",
        (company[0], company[1], document_id)).fetchall()
    return [int(row[0]) for row in rows]


def lacks_balance_sheet(connection: sqlite3.Connection, document_id: int) -> bool:
    """Kort dokument utan ord som hör till en balansräkning: inte värt ett AI-anrop."""
    row = connection.execute("SELECT page_count FROM reports WHERE document_id=?", (document_id,)).fetchone()
    if not row or not row[0] or row[0] >= SHORT_PAGES:
        return False
    text = " ".join(r[0] for r in connection.execute(
        "SELECT text FROM report_pages WHERE document_id=?", (document_id,)))
    return not BALANCE_WORDS.search(text)


def _skip(connection: sqlite3.Connection, document_id: int) -> None:
    with connection:
        connection.execute("UPDATE reports SET status='skipped', error=? WHERE document_id=?",
                           ("kort dokument utan balansräkning", document_id))
        connection.execute("DELETE FROM runway_results WHERE document_id=? AND "
                           "json_extract(result_json, '$.status') != 'missing_full_report'", (document_id,))


def other_pdfs(connection: sqlite3.Connection, document_id: int) -> list[str]:
    """Övriga PDF-bilagor till rapporten (utom den som redan används), i MFN:s ordning."""
    row = connection.execute("SELECT pdf_url, attachments FROM reports WHERE document_id=?",
                             (document_id,)).fetchone()
    try:
        attachments = json.loads(row[1]) if row and row[1] else []
    except ValueError:
        attachments = []
    return [url for url in attachments
            if url != row[0] and url.lower().split("?")[0].endswith(".pdf")]


def _try_other_pdfs(connection: sqlite3.Connection, settings: Settings, document_id: int, *, today: str) -> bool:
    """Första PDF:en var bara pressmeddelandet: pröva övriga bilagor. Sant när en har en balansräkning."""
    for url in other_pdfs(connection, document_id):
        with connection:
            connection.execute("UPDATE reports SET pdf_url=?, status='new', pdf_path=NULL, error=NULL "
                               "WHERE document_id=?", (url, document_id))
        process(connection, settings, None, document_id, today=today)
        if not lacks_balance_sheet(connection, document_id):
            return True
    return False


def process_with_fallback(connection: sqlite3.Connection, settings: Settings, client: ChatClient | None,
                          document_id: int, *, today: str) -> dict:
    """Senaste rapporten först; korta dokument utan siffror prövas mot rapportens övriga bilagor och
    hoppas annars över mot en äldre rapport."""
    outcome: dict = {}
    for candidate in company_reports(connection, document_id)[:MAX_FALLBACKS + 1]:
        status = connection.execute("SELECT status FROM reports WHERE document_id=?", (candidate,)).fetchone()[0]
        if status == "skipped":
            continue
        process(connection, settings, None, candidate, today=today)  # hämta och text, inget AI-anrop
        if lacks_balance_sheet(connection, candidate) and not _try_other_pdfs(
                connection, settings, candidate, today=today):
            _skip(connection, candidate)
            continue
        outcome = process(connection, settings, client, candidate, today=today, model=settings.deepseek_model)
        if (outcome.get("runway") or {}).get("status") == "ok":
            break
    if outcome:
        return outcome
    # Inget fullständigt underlag: behåll bolaget synligt som "saknar data".
    result = {"status": "missing_full_report", "flags": []}
    with connection:
        company_id = connection.execute("SELECT company_id FROM reports WHERE document_id=?",
                                        (document_id,)).fetchone()[0]
        connection.execute(
            """INSERT INTO runway_results(company_id, document_id, computed_at, result_json)
               VALUES (?, ?, datetime('now'), ?) ON CONFLICT(company_id, document_id)
               DO UPDATE SET result_json=excluded.result_json""",
            (company_id, document_id, json.dumps(result)))
    return {"document_id": document_id, "status": "failed", "error": "inga rapporter med balansräkning",
            "runway": result}


def latest_reports(connection: sqlite3.Connection, limit: int | None = None) -> list[tuple[int, str]]:
    rows = [(int(row[0]), str(row[1])) for row in connection.execute(LATEST_REPORTS)]
    return rows[:limit] if limit else rows


def tokens_used(connection: sqlite3.Connection, document_id: int | None = None) -> int:
    """Tokens för en rapport. Per rapport, så att parallella arbetare inte räknar varandras."""
    where, params = ("WHERE document_id=?", (document_id,)) if document_id is not None else ("", ())
    row = connection.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) FROM extractions "
        + where, params).fetchone()
    return int(row[0])


class Budget:
    def __init__(self, limit: int, used: int) -> None:
        self.limit, self.used = limit, used
        self._lock = threading.Lock()

    def exhausted(self) -> bool:
        with self._lock:
            return self.used >= self.limit

    def add(self, tokens: int) -> None:
        with self._lock:
            self.used += tokens


def run_latest(settings: Settings, client: ChatClient | None, *, today: str, workers: int = 4,
               limit: int | None = None, token_budget: int = 8_000_000,
               log: Callable[[str], None] = print) -> dict[str, int]:
    main = connect(settings.db_path)
    try:
        jobs = latest_reports(main, limit)
        budget = Budget(token_budget, 0)  # budgeten gäller den här körningen
    finally:
        main.close()
    counts: dict[str, int] = {}
    lock = threading.Lock()

    def work(document_id: int, name: str) -> str:
        connection = connect(settings.db_path)
        try:
            candidates = company_reports(connection, document_id)[:MAX_FALLBACKS + 1]
            before = sum(tokens_used(connection, d) for d in candidates)
            ai = None if budget.exhausted() else client
            outcome = process_with_fallback(connection, settings, ai, document_id, today=today)
            budget.add(sum(tokens_used(connection, d) for d in candidates) - before)
        finally:
            connection.close()
        if outcome.get("status") in ("failed", "too_long"):
            label = "fel: " + outcome["error"].split(":")[0]
        else:
            label = (outcome.get("runway") or {}).get("status", "ingen_extraktion")
            if ai is None and label == "missing_extraction":
                label = "budget_slut" if client else "ingen_nyckel"
        log(f"{label:24} {name}")
        return label

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed(pool.submit(work, d, n) for d, n in jobs):
            label = future.result()
            with lock:
                counts[label] = counts.get(label, 0) + 1
    counts["tokens_used"] = budget.used
    return counts
