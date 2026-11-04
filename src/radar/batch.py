"""Steg 2: kör kedjan för senaste rapporten per bolag, kostnadsmedvetet.

- Bara senaste rapporten med PDF per bolag.
- Redan extraherade rapporter hoppas över (inga nya AI-anrop).
- Flera arbetare parallellt, var och en med egen databasanslutning.
- Tokenbudget: inga nya extraktioner startas när budgeten är förbrukad.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .db import connect
from .extract import ChatClient
from .pipeline import process
from .settings import Settings

LATEST_REPORTS = """
SELECT r.document_id, c.name FROM reports r JOIN companies c ON c.id = r.company_id
WHERE r.pdf_url IS NOT NULL AND r.published_at = (
    SELECT MAX(r2.published_at) FROM reports r2 WHERE r2.company_id = r.company_id AND r2.pdf_url IS NOT NULL)
ORDER BY c.name COLLATE NOCASE
"""


def latest_reports(connection: sqlite3.Connection, limit: int | None = None) -> list[tuple[int, str]]:
    rows = [(int(row[0]), str(row[1])) for row in connection.execute(LATEST_REPORTS)]
    return rows[:limit] if limit else rows


def tokens_used(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) FROM extractions").fetchone()
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
        budget = Budget(token_budget, tokens_used(main))
    finally:
        main.close()
    counts: dict[str, int] = {}
    lock = threading.Lock()

    def work(document_id: int, name: str) -> str:
        connection = connect(settings.db_path)
        try:
            before = tokens_used(connection)
            ai = None if budget.exhausted() else client
            outcome = process(connection, settings, ai, document_id, today=today, model=settings.deepseek_model)
            budget.add(tokens_used(connection) - before)
        finally:
            connection.close()
        if outcome.get("status") == "failed":
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
