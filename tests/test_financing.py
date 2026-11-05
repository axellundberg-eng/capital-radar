"""Steg 4: finansieringshändelser i pressmeddelanden och deras plats i runway."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from dataclasses import replace
from pathlib import Path

import pytest

from radar import financing, news, pipeline
from radar.db import connect
from radar.extract import ExtractionError
from radar.fetch import visible_text
from radar.financing import looks_like_financing, parse_financing
from radar.importer import import_legacy
from radar.rounds import Event, dedupe, summarize
from radar.runway import Fact, compute
from radar.settings import Settings

END = "2026-06-30"
TODAY = "2026-09-30"


def event(kind="rights_issue", status="announced", gross=50e6, day="2026-08-01", net=None, currency="SEK"):
    return Event(kind, status, gross, net, currency, day, "citat", "Rubrik", "https://mfn.se/a/1")


def facts(cash=12e6, cfo=-6e6):
    return [Fact("cash", cash, None, END), Fact("operating_cash_flow", cfo, "2026-01-01", END)]


# --- Förfilter -----------------------------------------------------------------

def test_prefilter_accepts_financing_titles_in_both_languages():
    for title in ("Bolaget genomför en företrädesemission om 40 MSEK", "Beslut om riktad nyemission",
                  "Utfall i företrädesemissionen", "Bolaget tecknar brygglån", "Kapitalanskaffning slutförd",
                  "Emissionsgarantier klara", "Convertible loan agreement signed", "Rights issue outcome",
                  "Warrants exercised", "Private placement of SEK 20m", "Ny finansiering säkrad",
                  "Kallelse till extra bolagsstämma för beslut om nyemission"):
        assert looks_like_financing(title), title


def test_prefilter_rejects_other_news():
    for title in ("Kallelse till årsstämma", "Kommuniké från årsstämma", "Delårsrapport januari-juni",
                  "Interim report Q2 2026", "Bolaget tecknar order med långsiktig kund", "Outcome of annual general meeting",
                  "Ny VD utsedd", None):
        assert not looks_like_financing(title), title


# --- Tolkning av AI-svar ---------------------------------------------------------

def test_parse_financing_applies_units_and_defaults():
    raw = json.dumps({"is_financing": True, "events": [
        {"type": "rights_issue", "status": "completed", "gross_amount": "52,5", "net_amount": 48,
         "unit": "MSEK", "currency": "SEK", "date": "2026-09-01", "quote": "Utfall"},
        {"type": "mystery", "status": "announced", "gross_amount": 3000, "unit": "TSEK", "quote": "x"},
        {"type": "loan", "status": "bogus", "gross_amount": 1, "unit": "SEK"}]})
    first, second = parse_financing(f"```json\n{raw}\n```", "2026-09-02")
    assert (first.gross_amount, first.net_amount, first.date, first.currency) == (52.5e6, 48e6, "2026-09-01", "SEK")
    assert (second.type, second.gross_amount, second.date) == ("other", 3e6, "2026-09-02")


def test_parse_financing_negative_and_errors():
    assert parse_financing(json.dumps({"is_financing": False, "events": []}), None) == []
    with pytest.raises(ExtractionError):
        parse_financing("", None)
    missing_unit = {"is_financing": True, "events": [{"type": "loan", "status": "completed", "gross_amount": 5}]}
    with pytest.raises(ExtractionError):
        parse_financing(json.dumps(missing_unit), None)


def test_ask_retries_once_then_fails():
    good = json.dumps({"is_financing": False, "events": []})
    answers = iter(["", good])

    class Flaky:
        def complete(self, system, user):
            return next(answers), {"prompt_tokens": 10, "completion_tokens": 1}

    events, usage = financing.ask(Flaky(), "text", None)
    assert events == [] and usage["prompt_tokens"] == 20

    class Empty:
        def complete(self, system, user):
            return "", {}

    with pytest.raises(ExtractionError):
        financing.ask(Empty(), "text", None)


def test_visible_text_skips_scripts_and_limits_length():
    html = "<html><head><title>x</title><style>p{}</style></head><body><script>bad()</script><p>Hej</p><p>världen</p></body></html>"
    assert visible_text(html) == "Hej\nvärlden"
    assert len(visible_text("<p>" + "a" * 50_000 + "</p>")) == 12_000


# --- Rundor och runway -----------------------------------------------------------

def test_announcement_and_outcome_are_one_round_with_best_status():
    rounds = dedupe([event(status="announced", gross=50e6, day="2026-08-01"),
                     event(status="completed", gross=46e6, day="2026-09-10")])
    assert len(rounds) == 1 and rounds[0].status == "completed" and rounds[0].gross_amount == 46e6
    missing = dedupe([event(status="announced", gross=50e6), event(status="completed", gross=None, net=44e6,
                                                                   day="2026-09-10")])
    assert len(missing) == 1 and missing[0].gross_amount == 50e6 and missing[0].net_amount == 44e6


def test_different_rounds_stay_separate_and_cancelled_removes():
    assert len(dedupe([event(gross=50e6), event(gross=10e6, day="2026-08-05")])) == 2   # belopp skiljer
    assert len(dedupe([event(), event(kind="loan")])) == 2                                # typ skiljer
    assert len(dedupe([event(day="2026-01-10"), event(day="2026-08-01")])) == 2           # över 120 dagar
    assert dedupe([event(status="announced"), event(status="cancelled", day="2026-08-20")]) == []


def test_summary_counts_only_completed_after_period_end():
    events = [event(status="completed", gross=50e6, day="2026-08-10"),
              event(kind="loan", status="completed", gross=10e6, day="2026-08-12"),
              event(kind="directed_issue", status="completed", gross=99e6, net=90e6, day="2026-05-01"),
              event(kind="warrants", status="announced", gross=7e6, day="2026-09-01"),
              event(kind="convertible", status="completed", gross=5e6, day="2026-09-02", currency="EUR")]
    confirmed, announced, listing = summarize(events, END, "SEK")
    assert confirmed == 50e6 * 0.9 + 10e6          # aktier minus ca 10 % kostnader, lån brutto
    assert announced == 7e6
    assert len(listing) == 4                       # emissionen före periodslut är borta; EUR visas men summeras inte


def test_runway_adds_confirmed_cash_and_flags_announced_round():
    base = compute(facts(), END, today=TODAY)
    assert base.runway_today == 9.0 and base.confirmed_financing == 0 and base.flags == []
    done = summarize([event(status="completed", gross=10e6, net=9e6, day="2026-08-10")], END)
    result = compute(facts(), END, today=TODAY, financing=done)
    assert result.confirmed_financing == 9e6 and result.runway_today == 18.0
    assert "financing_received" in result.flags and "raise_announced" not in result.flags
    pending = summarize([event(status="announced", gross=20e6, day="2026-09-20")], END)
    flagged = compute(facts(), END, today=TODAY, financing=pending)
    assert flagged.runway_today == 9.0 and flagged.announced_financing == 20e6
    assert "raise_announced" in flagged.flags and "financing_received" not in flagged.flags


def test_confirmed_financing_clears_exhausted_cash():
    late = "2026-12-31"
    assert "cash_exhausted_since_report" in compute(facts(cash=3e6), END, today=late).flags
    done = summarize([event(status="completed", gross=20e6, day="2026-10-10")], END)
    result = compute(facts(cash=3e6), END, today=late, financing=done)
    assert "cash_exhausted_since_report" not in result.flags
    assert result.runway_today > 14   # 3 + 18 miljoner minus drygt 6 månaders burn


# --- Import, CLI och webb --------------------------------------------------------

@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "screener.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE issuers (id INTEGER PRIMARY KEY, canonical_name TEXT);
            CREATE TABLE securities (id INTEGER PRIMARY KEY, issuer_id INT, isin TEXT, symbol TEXT,
                                     market TEXT, segment TEXT, active INT);
            CREATE TABLE documents (id INTEGER PRIMARY KEY, issuer_id INT, title TEXT, published_at TEXT,
                                    canonical_url TEXT, profile_category TEXT);
            CREATE TABLE attachments (id INTEGER PRIMARY KEY, document_id INT, url TEXT);
            INSERT INTO issuers VALUES (1, 'Exempel AB');
            INSERT INTO securities VALUES (1, 1, 'SE0000000001', 'EX', 'First North', 'Growth', 1);
            INSERT INTO documents VALUES (10, 1, 'Delårsrapport Q2', '2026-08-20', NULL, 'report');
            INSERT INTO attachments VALUES (1, 10, 'https://storage.mfn.se/x/rapport.pdf');
            INSERT INTO documents VALUES (20, 1, 'Företrädesemission om 50 MSEK', '2026-09-01', 'https://mfn.se/a/20', NULL);
            INSERT INTO documents VALUES (21, 1, 'Utfall i företrädesemissionen', '2026-09-20', 'https://mfn.se/a/21', 'other');
            INSERT INTO documents VALUES (22, 1, 'Kallelse till årsstämma', '2026-09-05', 'https://mfn.se/a/22', 'other');
            INSERT INTO documents VALUES (23, 1, 'Riktad emission', '2026-05-01', 'https://mfn.se/a/23', 'other');
            INSERT INTO documents VALUES (24, 1, 'Nytt lån tecknat', '2026-09-10', 'https://example.org/x', 'other');
        """)
    return path


class ReportClient:
    def complete(self, system, user):
        answer = {"report_period_end": END, "unit": "MSEK", "currency": "SEK", "cash": {"value": 12, "date": END},
                  "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                                  "period_end": END}]}
        return json.dumps(answer), {"prompt_tokens": 100, "completion_tokens": 50}


class NewsClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        done = "Utfall" in user
        event_ = {"type": "rights_issue", "status": "completed" if done else "announced",
                  "gross_amount": 46 if done else 50, "unit": "MSEK", "currency": "SEK",
                  "date": "2026-09-20" if done else "2026-09-01", "quote": "citat"}
        return json.dumps({"is_financing": True, "events": [event_]}), {"prompt_tokens": 1000, "completion_tokens": 100}


@pytest.fixture()
def setup(tmp_path, legacy_db, monkeypatch):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde", "Balansräkning likvida medel 12"])
    pipeline.process(connection, settings, ReportClient(), 10, today=TODAY)
    fetched = []

    def fake_fetch(url, **_):
        if "example.org" in url:
            raise OSError("värden är inte tillåten")
        fetched.append(url)
        return f"Text för {url}"

    monkeypatch.setattr(news, "fetch_text", fake_fetch)
    return settings, connection, fetched


def test_import_news_is_idempotent_and_keeps_status(tmp_path, legacy_db):
    connection = connect(tmp_path / "radar.db")
    assert import_legacy(connection, legacy_db)["news"] == 5
    with connection:
        connection.execute("UPDATE news SET status='checked' WHERE document_id=20")
    import_legacy(connection, legacy_db)
    rows = dict(connection.execute("SELECT document_id, status FROM news").fetchall())
    assert len(rows) == 5 and rows[20] == "checked" and rows[21] == "new"
    assert 10 not in rows  # rapporter är inte nyheter


def test_cli_news_run_finds_round_and_recomputes_runway(setup, capsys, monkeypatch):
    import radar.__main__ as cli

    settings, connection, fetched = setup
    connection.close()
    client = NewsClient()
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli, "_client", lambda _settings: client)
    monkeypatch.setattr(cli, "date", type("FixedDate", (), {"today": staticmethod(lambda: date(2026, 9, 30))}))
    assert cli.main(["news", "--latest", "--workers", "1"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    counts = json.loads(out[-1])
    # 20, 21 och 24 passerar förfiltret (24 misslyckas vid hämtningen); 22 (årsstämma) och 23 (före periodslut) gör det inte
    assert client.calls == 2 and fetched == ["https://mfn.se/a/21", "https://mfn.se/a/20"]
    assert counts["financing"] == 2 and counts["failed"] == 1 and counts["financing_received"] == 1
    assert counts["tokens_used"] == 2 * 1100 and "Exempel AB" in out[0]
    connection = connect(settings.db_path)
    statuses = dict(connection.execute("SELECT document_id, status FROM news").fetchall())
    assert statuses[20] == statuses[21] == "financing" and statuses[22] == statuses[23] == "new"
    assert statuses[24] == "failed"
    result = json.loads(connection.execute("SELECT result_json FROM runway_results").fetchone()[0])
    assert result["confirmed_financing"] == 46e6 * 0.9 and result["runway_today"] == pytest.approx(50.4)
    assert len(result["financing_events"]) == 1 and result["financing_events"][0]["status"] == "completed"
    assert "financing_received" in result["flags"] and "raise_announced" not in result["flags"]


def test_news_second_run_makes_no_new_calls_and_respects_limits(setup):
    settings, connection, fetched = setup
    client = NewsClient()
    one = news.process_company_news(connection, settings, client, 1, END, max_items=1)
    assert client.calls == 1 and one["financing"] == 1 and fetched == ["https://mfn.se/a/21"]  # nyaste först
    rest = news.process_company_news(connection, settings, client, 1, END, budget=news.Budget(0, 0))
    assert client.calls == 1 and rest.get("budget_slut") == 1
    news.process_company_news(connection, settings, client, 1, END)
    again = news.process_company_news(connection, settings, client, 1, END)
    assert client.calls == 2 and again["checked"] == again["financing"] == 0
    assert news.candidates(connection, 1, END, retry_failed=True)[0]["document_id"] == 24


def test_web_exposes_financing_and_moves_refinanced_company_out_of_exhausted(setup):
    from fastapi.testclient import TestClient

    from radar.web import create_app

    settings, connection, _ = setup
    pipeline.refresh_runway(connection, 10, today="2027-09-30")
    client = TestClient(create_app(settings))
    assert client.get("/api/companies").json()["items"][0]["band"] == "exhausted"
    news.process_company_news(connection, settings, NewsClient(), 1, END)
    pipeline.refresh_runway(connection, 10, today="2027-09-30")
    item = client.get("/api/companies").json()["items"][0]
    assert item["band"] != "exhausted" and item["confirmed_financing"] == 46e6 * 0.9
    assert item["announced_financing"] == 0 and item["financing_events"][0]["url"] == "https://mfn.se/a/21"
    detail = client.get("/api/companies/1").json()
    assert {"confirmed_financing", "announced_financing", "financing_events"} <= set(detail)
    assert detail["financing_events"][0]["title"] == "Utfall i företrädesemissionen"
    html = client.get("/").text
    assert "hideRaise" in html and "Dölj bolag med annonserad emission" in html


def test_web_flags_announced_round(setup):
    from fastapi.testclient import TestClient

    from radar.web import create_app

    settings, connection, _ = setup
    with connection:
        connection.execute("UPDATE news SET status='checked' WHERE document_id=21")
    news.process_company_news(connection, settings, NewsClient(), 1, END)
    pipeline.refresh_runway(connection, 10, today=TODAY)
    item = TestClient(create_app(settings)).get("/api/companies").json()["items"][0]
    assert "raise_announced" in item["flags"] and item["announced_financing"] == 50e6
    assert item["confirmed_financing"] == 0 and item["runway_today"] == 9.0


def test_repayment_reduces_cash_and_prefilter_finds_it():
    from radar.financing import looks_like_financing
    from radar.rounds import Event, summarize

    assert looks_like_financing("Bolaget amorterar 50 MSEK på lånet")
    assert looks_like_financing("Company repays convertible loan")
    events = [Event("directed_issue", "completed", 30e6, 27e6, "SEK", "2026-08-01"),
              Event("repayment", "completed", 50e6, None, "SEK", "2026-08-15")]
    confirmed, announced, listing = summarize(events, "2026-06-30", "SEK")
    assert confirmed == 27e6 - 50e6
    assert {row["type"] for row in listing} == {"directed_issue", "repayment"}


def test_repayment_in_report_and_news_counts_once():
    from radar.rounds import Event, summarize
    from radar.runway import Fact, compute

    end = "2026-06-30"
    facts = [Fact("cash", 180e6, None, end), Fact("operating_cash_flow", -79e6, "2026-01-01", end),
             Fact("post_period_outflow", 50e6, "2026-08-01", end)]
    news = summarize([Event("repayment", "completed", 50e6, None, "SEK", "2026-08-01")], end, "SEK")
    once = compute(facts, end, today=end, financing=news)
    assert once.post_period_outflows == 0.0 and once.confirmed_financing == -50e6
    assert "raise_announced" not in once.flags
    report_only = compute(facts, end, today=end)
    assert report_only.runway_today == once.runway_today
