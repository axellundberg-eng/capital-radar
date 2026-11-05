"""Kommandon för den nya kärnan.

    python -m radar import                 läs bolag och rapportlänkar ur gamla databasen
    python -m radar run --document 123     kör kedjan för en rapport
    python -m radar run --facit            kör kedjan för facitsetet och jämför
    python -m radar run --latest           senaste rapporten per bolag (--workers, --limit, --token-budget)
    python -m radar news --latest          pressmeddelanden efter senaste rapporten: hitta finansiering
                                           (--max-runway 12, --workers, --token-budget, --max-items, --retry-failed)
    python -m radar qualify                hämta saknat/gammalt marknadsvärde (Nasdaq) för bolag med runway
                                           under 9 mån utan annonserad emission (--refresh-market tvingar om)
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
from .marketcap import refresh_many
from .news import run_news
from .pipeline import process
from .qualify import prospect_companies
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


def reextract(connection, document_id: int) -> None:
    """Låt en redan extraherad rapport extraheras igen (t.ex. efter ny prompt)."""
    with connection:
        connection.execute("UPDATE reports SET status='text' WHERE document_id=? AND status='extracted'",
                           (document_id,))


def run_facit(connection, settings: Settings, client, today: str, *, force: bool = False) -> int:
    cases = json.loads(FACIT.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for case in cases:
        if force and client is not None:
            reextract(connection, case["document_id"])
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


def news_command(args, connection, settings: Settings, client, today: str) -> int:
    if not args.latest:
        print("Ange --latest.", flush=True)
        return 2
    if client is None:
        print("Nyhetsläsningen kräver modellen (DeepSeek-nyckeln saknas).", flush=True)
        return 1
    connection.close()
    counts = run_news(settings, client, today=today, max_runway=args.max_runway, workers=args.workers,
                      token_budget=args.token_budget, max_items=args.max_items, retry_failed=args.retry_failed,
                      log=lambda line: print(line, flush=True))
    print(json.dumps(counts, ensure_ascii=False), flush=True)
    return 0


def qualify_command(args, connection, today: str) -> int:
    from .web import latest_rows

    wanted = [(i["id"], isin) for i, isin in prospect_companies(connection, latest_rows(connection))]
    counts = refresh_many(connection, wanted, today, force=args.refresh_market)
    counts["prospects"] = len(wanted)
    print(json.dumps(counts, ensure_ascii=False), flush=True)
    return 0


def schedule_command(args) -> int:
    import logging
    import os

    from .scheduler import loop, run_cycle

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    client = _client(settings)
    budgets = {"report_budget": int(os.getenv("RADAR_CYCLE_REPORT_TOKENS", "500000")),
               "news_budget": int(os.getenv("RADAR_CYCLE_NEWS_TOKENS", "300000"))}
    if args.once:
        print(json.dumps(run_cycle(settings, client, **budgets), ensure_ascii=False, default=str), flush=True)
        return 0
    loop(settings, client, interval_minutes=float(os.getenv("RADAR_SCHEDULE_MINUTES", "60")), **budgets)
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
    run.add_argument("--reextract", action="store_true", help="extrahera igen (facit/--document)")
    run.add_argument("--reextract-below", type=float, metavar="MÅN",
                     help="med --latest: extrahera igen bolag som bränner kassa och har runway under MÅN "
                          "månader (så plockas händelser efter perioden och kortfristiga lån upp)")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--limit", type=int)
    run.add_argument("--token-budget", type=int, default=8_000_000)
    news = sub.add_parser("news")
    news.add_argument("--latest", action="store_true", help="bolag med kort runway, nyheter efter senaste rapporten")
    news.add_argument("--max-runway", type=float, default=12.0, metavar="MÅN")
    news.add_argument("--workers", type=int, default=4)
    news.add_argument("--token-budget", type=int, default=1_000_000)
    news.add_argument("--max-items", type=int, default=8, help="högst så många AI-granskade nyheter per bolag")
    news.add_argument("--retry-failed", action="store_true", help="försök igen med nyheter som misslyckats")
    qualify = sub.add_parser("qualify")
    qualify.add_argument("--refresh-market", action="store_true", help="hämta marknadsvärde igen även om det är färskt")
    sub.add_parser("status")
    sub.add_parser("serve")
    schedule = sub.add_parser("schedule", help="löpande drift: import, rapporter, nyheter, marknadsvärde")
    schedule.add_argument("--once", action="store_true", help="kör ett varv och avsluta")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve()
    if args.command == "schedule":
        return schedule_command(args)

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
    if args.command == "qualify":
        return qualify_command(args, connection, today)
    client = _client(settings)
    if client is None:
        print("DEEPSEEK_API_KEY saknas; kör bara hämtning och text.")
    if args.command == "news":
        return news_command(args, connection, settings, client, today)
    if args.facit:
        return run_facit(connection, settings, client, today, force=args.reextract)
    if args.latest:
        if args.reextract_below is not None and client is not None:
            for document_id, result_json in connection.execute(
                    "SELECT document_id, result_json FROM runway_results").fetchall():
                result = json.loads(result_json)
                short = (result.get("status") == "ok" and not result.get("cash_flow_positive")
                         and (result.get("runway_today") or 0) < args.reextract_below)
                if short:
                    reextract(connection, document_id)
        connection.close()
        counts = run_latest(settings, client, today=today, workers=args.workers, limit=args.limit,
                            token_budget=args.token_budget, log=lambda line: print(line, flush=True))
        print(json.dumps(counts, ensure_ascii=False), flush=True)
        return 0
    for document_id in args.document:
        if args.reextract and client is not None:
            reextract(connection, document_id)
        print(json.dumps(process(connection, settings, client, document_id, today=today,
                                 model=settings.deepseek_model), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
