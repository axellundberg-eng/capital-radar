"""Kommandon för den nya kärnan.

    python -m radar import                 läs bolag och rapportlänkar ur gamla databasen
    python -m radar run --document 123     kör kedjan för en rapport
    python -m radar run --facit            kör kedjan för facitsetet och jämför
    python -m radar status                 antal rapporter per status
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .credentials import Credentials
from .db import connect
from .extract import DeepSeekClient
from .importer import import_legacy
from .pipeline import process
from .settings import Settings

FACIT = Path(__file__).resolve().parent / "data" / "runway_facit.json"
TOLERANCE = 0.2


def _client(settings: Settings):
    key = Credentials.from_env().deepseek_api_key
    if not key:
        return None
    return DeepSeekClient(key, model=settings.deepseek_model, base_url=settings.deepseek_base_url)


def _facit_check(case: dict, runway: dict) -> str:
    if runway.get("status") != "ok":
        return "saknas"
    if case.get("cash_flow_positive"):
        return "ok" if runway.get("cash_flow_positive") else "fel"
    expected = case.get("expected_monthly_burn")
    if not expected:
        return "ej verifierad"
    burn = runway.get("monthly_burn") or 0.0
    return "ok" if abs(burn - expected) <= TOLERANCE * expected else "fel"


def run_facit(connection, settings: Settings, client, today: str) -> int:
    cases = json.loads(FACIT.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for case in cases:
        outcome = process(connection, settings, client, case["document_id"], today=today, model=settings.deepseek_model)
        runway = outcome.get("runway") or {}
        verdict = _facit_check(case, runway) if "error" not in outcome else "steg-fel"
        counts[verdict] = counts.get(verdict, 0) + 1
        burn = runway.get("monthly_burn")
        expected = case.get("expected_monthly_burn")
        print(f"{verdict:14} {case['name'][:24]:24} burn {burn if burn is None else round(burn):>12} "
              f"facit {expected if expected is None else round(expected):>12}  {outcome.get('error') or ''}")
    print(json.dumps(counts, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import")
    run = sub.add_parser("run")
    run.add_argument("--document", type=int, action="append", default=[])
    run.add_argument("--facit", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    connection = connect(settings.db_path)
    today = date.today().isoformat()
    if args.command == "import":
        print(json.dumps(import_legacy(connection, settings.legacy_db_path)))
        return 0
    if args.command == "status":
        rows = connection.execute("SELECT status, COUNT(*) FROM reports GROUP BY status").fetchall()
        print(json.dumps({row[0]: row[1] for row in rows}))
        return 0
    client = _client(settings)
    if client is None:
        print("DEEPSEEK_API_KEY saknas; kör bara hämtning och text.")
    if args.facit:
        return run_facit(connection, settings, client, today)
    for document_id in args.document:
        print(json.dumps(process(connection, settings, client, document_id, today=today,
                                 model=settings.deepseek_model), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
