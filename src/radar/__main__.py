"""Kommandon för den nya kärnan.

    python -m radar import                 läs bolag och rapportlänkar ur gamla databasen
    python -m radar collect                hämta och klassificera nya MFN-poster (--workers, --limit)
                                           --dry-run: skriv inget; --compare-legacy DAGAR: jämför mot gamla databasen
    python -m radar run --document 123     kör kedjan för en rapport
    python -m radar run --facit            kör kedjan för facitsetet och jämför
    python -m radar run --latest           senaste rapporten per bolag (--workers, --limit, --token-budget)
    python -m radar news --latest          pressmeddelanden efter senaste rapporten: hitta finansiering
                                           (--max-runway 12, --workers, --token-budget, --max-items, --retry-failed)
    python -m radar qualify                hämta saknat/gammalt marknadsvärde (Nasdaq) för bolag med runway
                                           under 9 mån utan annonserad emission (--refresh-market tvingar om)
    python -m radar status                 antal rapporter per status
    python -m radar serve                  webbgränssnitt (RADAR_PORT, RADAR_BASE_PATH)

Granskning (se agents/radar-reviewer.md):
    python -m radar review checks          rimlighetskontroller, sammanfattning (--json för alla flaggor)
    python -m radar review queue           bolag att granska, troligaste felen först (--since, --limit)
    python -m radar review log             logga en granskningsrunda (--checked, --corrections, --before, --note)
    python -m radar correct fact --document D --kind cash --period-end 2026-09-30 --value 8612000 --reason "..."
    python -m radar correct fact --document D --kind operating_cash_flow --period-start ... --delete --reason "..."
    python -m radar correct period --document D --period-end 2026-09-30 --reason "..."
    python -m radar correct scale --document D --factor 1000 --reason "..."   hela rapporten i fel enhet
    python -m radar correct event --id N --hide | --status S | --type T | --amount A | --currency C
                                  | --date D | --advisers "..." --reason "..."
    python -m radar corrections list [--company ID]  |  corrections revert ID
    python -m radar fx                     hämta dagens växelkurser från Riksbanken (högst en gång per dag)
    python -m radar market [--limit N]     kurshistorik och omsättning från Nasdaq (högst en gång per dag och bolag)
    python -m radar insiders [--limit N]   insynshandel från Finansinspektionen (senaste 12 mån)
    python -m radar slugs [--apply]        matcha bolag utan MFN-sida; utan --apply skrivs bara förslag ut
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


def review_command(args, connection, today: str) -> int:
    from . import review

    if args.action == "checks":
        findings = review.run_checks(connection, today)
        out = {"summary": review.summary(findings)}
        if args.json:
            out["findings"] = review.as_dicts(findings[:args.limit] if args.limit else findings)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.action == "queue":
        print(json.dumps(review.queue(connection, args.since, args.limit), ensure_ascii=False, indent=1))
        return 0
    after = len(review.run_checks(connection, today))
    run_id = review.log_run(connection, since=args.since or review.since_default(connection), checked=args.checked,
                            corrections=args.corrections, findings_before=args.before, findings_after=after,
                            note=args.note)
    print(json.dumps({"review_run": run_id, "findings_after": after}, ensure_ascii=False))
    return 0


def _runway_brief(result: dict) -> dict:
    keys = ("status", "period_end", "cash", "monthly_burn", "runway_today", "runway_cash_today", "cash_flow_positive")
    return {k: result.get(k) for k in keys}


def correct_command(args, connection, today: str) -> int:
    from . import review
    from .pipeline import refresh_runway

    if args.command == "corrections":
        if args.action == "list":
            sql, params = "SELECT * FROM corrections", []
            if args.company:
                sql, params = sql + " WHERE company_id=?", [args.company]
            print(json.dumps([dict(r) for r in connection.execute(sql + " ORDER BY id", params)],
                             ensure_ascii=False, indent=1))
            return 0
        if args.id is None:
            raise ValueError("ange id för rättelsen som ska ångras")
        row = review.revert_correction(connection, args.id)
        document = row["document_id"] if row["target"] == "fact" else review.latest_document(connection, row["company_id"])
        result = refresh_runway(connection, document, today=today) if document else {}
        print(json.dumps({"reverted": args.id, "runway": _runway_brief(result)}, ensure_ascii=False))
        return 0
    if args.target == "scale":
        if args.document is None or not args.factor:
            raise ValueError("--document och --factor krävs")
        company = connection.execute("SELECT company_id FROM reports WHERE document_id=?", (args.document,)).fetchone()
        if company is None:
            raise ValueError(f"rapport {args.document} finns inte")
        ids = [review.add_correction(connection, target="fact", field="scale", reason=args.reason,
                                     company_id=company[0], document_id=args.document, new_value=args.factor)]
        result = refresh_runway(connection, args.document, today=today)
        print(json.dumps({"corrections": ids, "runway": _runway_brief(result)}, ensure_ascii=False))
        return 0
    if args.target in ("fact", "period"):
        if args.document is None:
            raise ValueError("--document krävs")
        company = connection.execute("SELECT company_id FROM reports WHERE document_id=?", (args.document,)).fetchone()
        if company is None:
            raise ValueError(f"rapport {args.document} finns inte")
        if args.target == "period":
            if not args.period_end:
                raise ValueError("--period-end krävs")
            ids = [review.add_correction(connection, target="fact", field="report_period_end", reason=args.reason,
                                         company_id=company[0], document_id=args.document, new_value=args.period_end)]
        else:
            if not args.kind or (args.value is None and not args.delete):
                raise ValueError("--kind och antingen --value eller --delete krävs")
            ids = [review.add_correction(
                connection, target="fact", field="delete" if args.delete else "value", reason=args.reason,
                company_id=company[0], document_id=args.document, kind=args.kind, period_start=args.period_start,
                period_end=args.period_end, new_value=None if args.delete else args.value)]
        result = refresh_runway(connection, args.document, today=today)
        print(json.dumps({"corrections": ids, "runway": _runway_brief(result)}, ensure_ascii=False))
        return 0
    if args.id is None:
        raise ValueError("--id krävs för en händelse")
    event = connection.execute("SELECT company_id FROM financing_events WHERE id=?", (args.id,)).fetchone()
    if event is None:
        raise ValueError(f"händelse {args.id} finns inte")
    changes = {"hide": "1" if args.hide else None, "status": args.status, "type": args.event_type,
               "gross_amount": args.amount, "currency": args.currency, "event_date": args.date,
               "advisers": args.advisers}
    changes = {field: value for field, value in changes.items() if value is not None}
    if not changes:
        raise ValueError("ange vad som ska rättas: --hide, --status, --type, --amount, --currency, --date eller --advisers")
    ids = [review.add_correction(connection, target="event", field=field, reason=args.reason, company_id=event[0],
                                 event_id=args.id, new_value=value) for field, value in changes.items()]
    document = review.latest_document(connection, event[0])
    result = refresh_runway(connection, document, today=today) if document else {}
    print(json.dumps({"corrections": ids, "runway": _runway_brief(result)}, ensure_ascii=False))
    return 0


def schedule_command(args) -> int:
    import logging
    import os

    from .scheduler import loop, run_cycle

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    client = _client(settings)
    budgets = {"report_budget": int(os.getenv("RADAR_CYCLE_REPORT_TOKENS", "200000")),
               "news_budget": int(os.getenv("RADAR_CYCLE_NEWS_TOKENS", "100000"))}
    if args.once:
        print(json.dumps(run_cycle(settings, client, **budgets), ensure_ascii=False, default=str), flush=True)
        return 0
    loop(settings, client, interval_minutes=float(os.getenv("RADAR_SCHEDULE_MINUTES", "60")), **budgets)
    return 0


def collect_command(args) -> int:
    from .collect import collect_all
    from .compare import compare_legacy

    settings = Settings.from_env()
    if args.compare_legacy is not None:
        result = compare_legacy(settings, args.compare_legacy, workers=args.workers,
                                log=lambda line: print(line, flush=True))
    else:
        result = collect_all(settings, args.workers, lambda line: print(line, flush=True),
                             limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
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
        outer.add_api_route("/", lambda: RedirectResponse(base + "/"), include_in_schema=False)
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
    collect = sub.add_parser("collect", help="samla MFN-poster själv i stället för att importera")
    collect.add_argument("--workers", type=int, default=4)
    collect.add_argument("--limit", type=int, help="högst så många bolag")
    collect.add_argument("--dry-run", action="store_true", help="skriv ingenting till radar.db")
    collect.add_argument("--compare-legacy", type=int, metavar="DAGAR",
                         help="jämför mot gamla databasens dokument de senaste DAGAR dagarna (skriver inget)")
    sub.add_parser("status")
    sub.add_parser("serve")
    schedule = sub.add_parser("schedule", help="löpande drift: import, rapporter, nyheter, marknadsvärde")
    schedule.add_argument("--once", action="store_true", help="kör ett varv och avsluta")
    review = sub.add_parser("review", help="granskning: kontroller, kö och logg")
    review.add_argument("action", choices=["checks", "queue", "log"])
    review.add_argument("--since", help="datum (YYYY-MM-DD); standard: förra granskningen eller 7 dagar bakåt")
    review.add_argument("--limit", type=int)
    review.add_argument("--json", action="store_true", help="checks: skriv alla flaggor som JSON")
    review.add_argument("--checked", type=int, default=0)
    review.add_argument("--corrections", type=int, default=0)
    review.add_argument("--before", type=int, help="antal flaggor före rundan")
    review.add_argument("--note", default="")
    correct = sub.add_parser("correct", help="rätta ett faktum, ett periodslut eller en händelse (kräver --reason)")
    correct.add_argument("target", choices=["fact", "period", "event", "scale"])
    correct.add_argument("--factor", type=float, help="scale: multiplicera alla belopp i rapporten, t.ex. 1000")
    correct.add_argument("--reason", required=True)
    correct.add_argument("--document", type=int)
    correct.add_argument("--kind")
    correct.add_argument("--period-start")
    correct.add_argument("--period-end")
    correct.add_argument("--value", type=float)
    correct.add_argument("--delete", action="store_true")
    correct.add_argument("--id", type=int, help="händelsens id (financing_events.id)")
    correct.add_argument("--hide", action="store_true")
    correct.add_argument("--status", choices=["announced", "completed", "cancelled"])
    correct.add_argument("--type", dest="event_type")
    correct.add_argument("--amount", type=float)
    correct.add_argument("--currency")
    correct.add_argument("--date")
    correct.add_argument("--advisers")
    sub.add_parser("fx", help="hämta dagens växelkurser från Riksbanken")
    for name, text in (("market", "kurshistorik och omsättning från Nasdaq"),
                       ("insiders", "insynshandel från Finansinspektionen")):
        extra = sub.add_parser(name, help=text)
        extra.add_argument("--limit", type=int)
    slugs = sub.add_parser("slugs", help="matcha bolag utan MFN-sida mot MFN:s sökning")
    slugs.add_argument("--apply", action="store_true", help="skriv träffar på ISIN och exakt namn")
    corrections = sub.add_parser("corrections", help="lista eller ångra rättelser")
    corrections.add_argument("action", choices=["list", "revert"])
    corrections.add_argument("id", type=int, nargs="?")
    corrections.add_argument("--company", type=int)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve()
    if args.command == "schedule":
        return schedule_command(args)
    if args.command == "collect":
        return collect_command(args)

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
    if args.command == "review":
        return review_command(args, connection, today)
    if args.command in ("market", "insiders"):
        from . import insiders, market

        module = market if args.command == "market" else insiders
        print(json.dumps(module.run(connection, settings, limit=args.limit, log=lambda _line: None),
                         ensure_ascii=False, default=str))
        return 0
    if args.command == "slugs":
        from . import slugs as slug_matcher

        proposals = slug_matcher.dry_run(connection, settings, log=lambda *_: None)
        out = {"proposals": [{k: p.get(k) for k in ("id", "name", "isin", "slug", "method", "score")} for p in proposals]}
        if args.apply:
            out["applied"] = slug_matcher.apply(connection, proposals)
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return 0
    if args.command == "fx":
        from . import fx

        print(json.dumps({"refresh": fx.refresh(connection), "rates": fx.latest_rates(connection)}, ensure_ascii=False))
        return 0
    if args.command in ("correct", "corrections"):
        try:
            return correct_command(args, connection, today)
        except ValueError as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 2
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
