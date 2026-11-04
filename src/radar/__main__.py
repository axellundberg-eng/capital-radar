"""Kommandon för den nya kärnan.

    python -m radar import                 läs bolag och rapportlänkar ur gamla databasen
    python -m radar run --document 123     kör kedjan för en rapport
    python -m radar run --facit            kör kedjan för facitsetet och jämför
    python -m radar run --latest           senaste rapporten per bolag (--workers, --limit, --token-budget)
    python -m radar status                 antal rapporter per status
    python -m radar serve                  webbgränssnitt (RADAR_PORT, RADAR_BASE_PATH)
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .credentials import Credentials
from .db import connect
from .extract import DeepSeekClient
from .batch import run_latest
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
        def fmt(value):
            return "—" if value is None else f"{round(value):,}".replace(",", " ")
        print(f"{verdict:14} {case['name'][:24]:24} burn {fmt(burn):>12} facit {fmt(expected):>12}  "
              f"{outcome.get('error') or ''}", flush=True)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


def serve() -> int:
    import os

    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse

    from .web import create_app

    base = os.getenv("RADAR_BASE_PATH", "").rstrip("/")
    app = create_app()
    if base:
        outer = FastAPI()
        outer.add_api_route(base, lambda: RedirectResponse(base + "/"), include_in_schema=False)
        outer.mount(base, app)
        app = outer
    uvicorn.run(app, host=os.getenv("RADAR_HOST", "0.0.0.0"), port=int(os.getenv("RADAR_PORT", "8001")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import")
    run = sub.add_parser("run")
    run.add_argument("--document", type=int, action="append", default=[])
    run.add_argument("--facit", action="store_true")
    run.add_argument("--latest", action="store_true", help="senaste rapporten per bolag")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--limit", type=int)
    run.add_argument("--token-budget", type=int, default=8_000_000)
    sub.add_parser("status")
    sub.add_parser("serve")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve()

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
    if args.latest:
        connection.close()
        counts = run_latest(settings, client, today=today, workers=args.workers, limit=args.limit,
                            token_budget=args.token_budget, log=lambda line: print(line, flush=True))
        print(json.dumps(counts, ensure_ascii=False), flush=True)
        return 0
    for document_id in args.document:
        print(json.dumps(process(connection, settings, client, document_id, today=today,
                                 model=settings.deepseek_model), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
