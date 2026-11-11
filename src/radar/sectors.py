"""Bransch per bolag: ICB-bransch från Nasdaq och en underbransch för filtrering.

- ICB-branschen ("Health Care", "Technology" …) finns i den gamla databasens värdepappersdata och
  läses utan AI-anrop (`import_icb`). Den saknas för NGM-bolag.
- ICB skiljer inte medtech från bioteknik eller mjukvara från hårdvara. Underbranschen sätts därför av
  DeepSeek ur bolagets namn, ICB-bransch och början av senaste rapporten, till en fast lista (`classify`).
  Varje bolag klassas en gång; nya bolag klassas när de dyker upp.
- Ett manuellt val (`set_manual`) skrivs aldrig över.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .extract import ChatClient, ExtractionError, json_object

# (nyckel, etikett, grupp). Grupperna life_science och tech är Bergs fokus (prospektpoängens sektorkontroll).
TAXONOMY = (
    ("medtech", "Medtech", "life_science"),
    ("biotech", "Bioteknik och läkemedel", "life_science"),
    ("diagnostics", "Diagnostik", "life_science"),
    ("software", "Mjukvara och SaaS", "tech"),
    ("hardware", "Hårdvara och elektronik", "tech"),
    ("gaming", "Gaming", "tech"),
    ("fintech", "Fintech", "tech"),
    ("other_tech", "Övrig tech", "tech"),
    ("cleantech", "Cleantech och energi", "other"),
    ("mining", "Gruv och råvaror", "other"),
    ("industry", "Industri", "other"),
    ("consumer", "Konsument", "other"),
    ("real_estate", "Fastigheter", "other"),
    ("finance", "Finans och investment", "other"),
    ("other", "Övrigt", "other"),
)
LABEL = {key: label for key, label, _group in TAXONOMY}
GROUP = {key: group for key, _label, group in TAXONOMY}
TEXT_CHARS = 5000

PROMPT = """Du klassar ett svenskt börsbolag i exakt en underbransch utifrån vad bolaget gör.
Svara med JSON: {"subsector": "<nyckel>", "reason": "kort motivering på svenska, högst 15 ord"}.
Tillåtna nycklar:
- medtech: medicintekniska produkter, instrument, implantat, vårdteknik
- biotech: läkemedelsutveckling, bioteknik, terapier, vacciner
- diagnostics: diagnostik, tester, laboratorie- och analysverktyg för vården
- software: mjukvara, SaaS, plattformar, IT-tjänster
- hardware: elektronik, sensorer, hårdvara, telekomutrustning, rymd
- gaming: spelutveckling och spelförlag
- fintech: betalningar, finansiell teknik
- other_tech: annan teknik som inte passar ovan
- cleantech: förnybar energi, energilagring, miljöteknik, vätgas
- mining: gruvor, prospektering, råvaror
- industry: industri, tillverkning, bygg, transport, försvar
- consumer: konsumentprodukter, handel, livsmedel, hälsa och skönhet för konsumenter
- real_estate: fastigheter
- finance: investmentbolag, bank, försäkring, kapitalförvaltning
- other: allt annat
Välj den nyckel som bäst beskriver huvudverksamheten."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def import_icb(connection: sqlite3.Connection, legacy_db: Path) -> dict[str, int]:
    """Läs ICB-bransch per ISIN ur den gamla databasens värdepappersdata (metadata_json.sector)."""
    counts = {"matched": 0, "missing": 0}
    if not Path(legacy_db).exists():
        return {**counts, "legacy_missing": 1}
    source = sqlite3.connect(f"file:{Path(legacy_db).as_posix()}?mode=ro", uri=True)
    try:
        by_isin: dict[str, str] = {}
        for isin, meta in source.execute("SELECT isin, metadata_json FROM securities WHERE metadata_json IS NOT NULL"):
            try:
                sector = json.loads(meta).get("sector")
            except (ValueError, AttributeError):
                sector = None
            if isin and sector:
                by_isin.setdefault(isin, sector)
    finally:
        source.close()
    with connection:
        for company_id, isin in connection.execute("SELECT id, isin FROM companies").fetchall():
            icb = by_isin.get(isin or "")
            if not icb:
                counts["missing"] += 1
                continue
            connection.execute(
                """INSERT INTO company_sectors(company_id, icb, updated_at) VALUES (?,?,?)
                   ON CONFLICT(company_id) DO UPDATE SET icb=excluded.icb, updated_at=excluded.updated_at""",
                (company_id, icb, _now()))
            counts["matched"] += 1
    return counts


def _company_text(connection: sqlite3.Connection, company_id: int) -> str:
    """Början av senaste lästa rapporten (där verksamheten oftast beskrivs)."""
    row = connection.execute(
        """SELECT r.document_id FROM reports r WHERE r.company_id=? AND EXISTS
           (SELECT 1 FROM report_pages p WHERE p.document_id=r.document_id)
           ORDER BY r.published_at DESC LIMIT 1""", (company_id,)).fetchone()
    if not row:
        return ""
    text = " ".join(t for (t,) in connection.execute(
        "SELECT text FROM report_pages WHERE document_id=? ORDER BY page LIMIT 3", (row[0],)))
    return " ".join(text.split())[:TEXT_CHARS]


def parse_answer(raw: str) -> tuple[str, str]:
    data = json_object(raw)
    key = str(data.get("subsector", "")).strip().lower()
    if key not in LABEL:
        raise ExtractionError(f"okänd underbransch: {key!r}")
    return key, str(data.get("reason", ""))[:200]


def classify(connection: sqlite3.Connection, client: ChatClient, *, limit: int = 60,
             token_budget: int = 80_000) -> dict[str, int]:
    """Sätt underbransch för bolag som saknar en (aldrig över ett manuellt val)."""
    rows = connection.execute(
        """SELECT c.id, c.name, s.icb FROM companies c LEFT JOIN company_sectors s ON s.company_id = c.id
           WHERE s.subsector IS NULL ORDER BY c.id""").fetchall()
    counts = {"candidates": len(rows), "classified": 0, "failed": 0, "tokens": 0, "no_text": 0}
    for company_id, name, icb in rows[:limit]:
        if counts["tokens"] >= token_budget:
            counts["budget_slut"] = 1
            break
        text = _company_text(connection, company_id)
        if not text and not icb:
            counts["no_text"] += 1
            continue
        message = f"Bolag: {name}\nICB-bransch: {icb or 'okänd'}\nUr senaste rapporten:\n{text}"
        try:
            raw, usage = client.complete(PROMPT, message)
            counts["tokens"] += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            key, reason = parse_answer(raw)
        except Exception:  # noqa: BLE001 - ett bolag får inte stoppa övriga; försöks igen nästa varv
            counts["failed"] += 1
            continue
        with connection:
            connection.execute(
                """INSERT INTO company_sectors(company_id, subsector, source, reason, updated_at)
                   VALUES (?,?,'ai',?,?) ON CONFLICT(company_id) DO UPDATE SET subsector=excluded.subsector,
                   source='ai', reason=excluded.reason, updated_at=excluded.updated_at
                   WHERE company_sectors.source IS NOT 'manual'""",
                (company_id, key, reason, _now()))
        counts["classified"] += 1
    return counts


def set_manual(connection: sqlite3.Connection, company_id: int, key: str) -> None:
    """Sätt underbransch för hand. Den skrivs aldrig över av klassningen."""
    if key not in LABEL:
        raise ValueError(f"okänd underbransch {key!r}; tillåtna: {', '.join(LABEL)}")
    with connection:
        connection.execute(
            """INSERT INTO company_sectors(company_id, subsector, source, reason, updated_at)
               VALUES (?,?,'manual','satt för hand',?) ON CONFLICT(company_id) DO UPDATE SET
               subsector=excluded.subsector, source='manual', reason=excluded.reason, updated_at=excluded.updated_at""",
            (company_id, key, _now()))


def load(connection: sqlite3.Connection) -> dict[int, dict]:
    """Per bolag: icb, subsector, label och group (None där det saknas)."""
    try:
        rows = connection.execute("SELECT company_id, icb, subsector FROM company_sectors").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {company_id: {"icb": icb, "subsector": sub, "label": LABEL.get(sub), "group": GROUP.get(sub)}
            for company_id, icb, sub in rows}
