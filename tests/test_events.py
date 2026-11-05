"""Strukturella händelser efter rapporten: förfilter, tolkning, flaggor, band, nyhetssteg och webb."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_financing import END, ReportClient, legacy_db, setup  # noqa: F401 - fixtures

from radar import events, news, news_events
from radar.db import connect
from radar.events import StructuralEvent, dedupe, looks_structural, parse_events, summarize
from radar.extract import ExtractionError
from radar.runway import Fact, compute
from radar.web import band

NOW = "2026-11-05"
HEMPLY = ("Tillträdesvillkoren för det omvända förvärvet av Gotmic uppfyllda",
          "Hemply Balance Holding AB slutför omvänt förvärv av Gotmic AB",
          "Hemply Balance Holding AB genomför namnbyte till Gotmic Semiconductor AB")


def ev(kind="reverse_acquisition", status="completed", day="2026-11-03", counterparty=None):
    return StructuralEvent(kind, status, day, counterparty, "citat", "Rubrik", "https://mfn.se/a/9")


# --- Förfilter -----------------------------------------------------------------

def test_prefilter_accepts_real_hemply_titles_and_other_structural_words():
    for title in HEMPLY + (
            "Company completes reverse takeover", "Fusion mellan X och Y", "Merger agreement signed",
            "Offentligt uppköpserbjudande på Bolaget", "Public offer to the shareholders", "Tender offer announced",
            "Bolaget ansöker om avnotering", "Change of listing to Nasdaq First North", "Byte av handelsplats",
            "Bolaget ansöker om konkurs", "Company files for bankruptcy", "Företagsrekonstruktion inleds",
            "Beslut om likvidation", "Bolaget förvärvar rörelse", "Acquisition of Target Ltd",
            "Avknoppning av dotterbolaget", "Spin-off of subsidiary", "Name change effective",
            "Kallelse till extra bolagsstämma för godkännande av omvänt förvärv"):
        assert looks_structural(title), title


def test_prefilter_rejects_noise():
    for title in ("Kallelse till årsstämma", "Kommuniké från årsstämma", "Delårsrapport januari-september 2026",
                  "Interim report Q3 2026", "Ny VD utsedd", "Utfall i företrädesemissionen", "Public offering of shares",
                  "Bolaget förvärvar egna aktier", "Anmälan om transaktion: förvärv av aktier",
                  "Aktiesplit: uppdelning av aktier", "Bokslutskommuniké med förvärv", None):
        assert not looks_structural(title), title


# --- Tolkning ------------------------------------------------------------------

def test_parse_events_reads_types_status_and_defaults():
    raw = json.dumps({"is_structural": True, "events": [
        {"type": "reverse_acquisition", "status": "completed", "date": "2026-11-03", "counterparty": "Gotmic AB",
         "quote": "slutför"},
        {"type": "mystery", "status": "announced", "counterparty": "null"},
        {"type": "merger", "status": "bogus"}]})
    first, second = parse_events(raw, "2026-11-04")
    assert (first.type, first.status, first.date, first.counterparty) == (
        "reverse_acquisition", "completed", "2026-11-03", "Gotmic AB")
    assert (second.type, second.date, second.counterparty) == ("other", "2026-11-04", None)
    assert parse_events('{"is_structural": false, "events": []}', None) == []
    with pytest.raises(ExtractionError):
        parse_events("inte json", None)


def test_ask_retries_once_then_fails():
    class Bad:
        calls = 0

        def complete(self, system, user):
            self.calls += 1
            return "skräp", {"prompt_tokens": 10, "completion_tokens": 1}

    client = Bad()
    with pytest.raises(ExtractionError):
        events.ask(client, "text", None)
    assert client.calls == 2


# --- Dedupe och flaggor ----------------------------------------------------------

def test_announced_and_completed_are_one_event_with_best_status():
    merged = dedupe([ev(status="announced", day="2026-10-30"),
                     ev(status="completed", day="2026-11-03", counterparty="Gotmic AB")])
    assert len(merged) == 1 and merged[0].status == "completed" and merged[0].counterparty == "Gotmic AB"
    assert len(dedupe([ev(), ev("name_change", day="2026-11-05")])) == 2
    assert dedupe([ev(status="announced"), ev(status="cancelled")]) == []


def test_flags_follow_types_and_period_end():
    listing, flags = summarize([ev(), ev("name_change", day="2026-11-05")], END)
    assert flags == ["structural_change"] and [e["type"] for e in listing] == ["name_change", "reverse_acquisition"]
    assert summarize([ev("name_change")], END)[1] == []                       # namnbyte ensamt flaggar inte
    assert summarize([ev(day="2026-05-01")], END) == ([], [])                  # före periodslut
    assert summarize([ev(status="cancelled")], END) == ([], [])
    assert summarize([ev("public_offer", "announced")], END)[1] == ["public_offer"]
    assert summarize([ev("delisting")], END)[1] == ["delisting"]
    for kind in ("merger", "spin_off", "reorganisation", "liquidation", "bankruptcy"):
        assert summarize([ev(kind, "announced")], END)[1] == ["structural_change"], kind


def test_compute_records_events_without_changing_numbers():
    base = [Fact("cash", 12e6, None, END), Fact("operating_cash_flow", -6e6, "2026-01-01", END)]
    plain = compute(base, END, today=NOW)
    flagged = compute(base, END, today=NOW, structural=summarize([ev()], END))
    assert "structural_change" in flagged.flags and flagged.structural_events[0]["url"] == "https://mfn.se/a/9"
    assert flagged.runway_today == plain.runway_today and "structural_change" not in plain.flags


def test_band_precedence():
    row = {"status": "ok", "cash_flow_positive": False, "runway_today": 0.0,
           "flags": ["structural_change", "raise_announced", "cash_exhausted_since_report", "acute"]}
    assert band(row) == "structural"
    assert band({**row, "flags": ["raise_announced", "acute"]}) == "raising"
    assert band({**row, "status": "missing_cash"}) == "missing"
    assert band({**row, "cash_flow_positive": True}) == "positive"
    assert band({**row, "flags": ["public_offer", "delisting"], "runway_today": 5.0}) == "structural"
    assert band({**row, "flags": ["acquisition"], "runway_today": 5.0}) == "3_9"


# --- Nyhetssteget och webben ------------------------------------------------------

class EventClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if "namnbyte" in user:
            item = {"type": "name_change", "status": "completed", "date": "2026-11-05",
                    "counterparty": "Gotmic Semiconductor AB", "quote": "namnbyte"}
        else:
            done = "slutför" in user
            item = {"type": "reverse_acquisition", "status": "completed" if done else "announced",
                    "date": "2026-11-03" if done else "2026-10-30", "counterparty": "Gotmic AB",
                    "quote": "omvänt förvärv"}
        return json.dumps({"is_structural": True, "events": [item]}), {"prompt_tokens": 800, "completion_tokens": 100}


@pytest.fixture()
def hemply(setup, monkeypatch):  # noqa: F811
    settings, connection, _ = setup
    fetched = []

    def fake_fetch(url, **_):
        fetched.append(url)
        return "Text för " + HEMPLY[2] if url.endswith("/32") else f"Text för {url}"

    monkeypatch.setattr(news_events, "fetch_text", fake_fetch)
    rows = [(30, "2026-10-30", HEMPLY[0]), (31, "2026-11-03", HEMPLY[1]), (32, "2026-11-05", HEMPLY[2]),
            (33, "2026-05-01", "Fusion före rapporten"), (34, "2026-11-04", "Kallelse till årsstämma")]
    with connection:
        connection.executemany(
            "INSERT INTO news(document_id, company_id, title, published_at, url) VALUES (?,?,?,?,?)",
            [(i, 1, title, day, f"https://mfn.se/a/{i}") for i, day, title in rows])
    return settings, connection, fetched


def test_event_step_judges_each_news_once_and_respects_limits(hemply):
    settings, connection, fetched = hemply
    client = EventClient()
    one = news_events.process_company_events(connection, settings, client, 1, END, max_items=1)
    assert client.calls == 1 and fetched == ["https://mfn.se/a/32"] and one["events_tokens"] == 900  # nyaste först
    out = news_events.process_company_events(connection, settings, client, 1, END, budget=news.Budget(0, 0))
    assert client.calls == 1 and out["budget_slut"] == 1
    news_events.process_company_events(connection, settings, client, 1, END)
    again = news_events.process_company_events(connection, settings, client, 1, END)
    assert client.calls == 3 and again["structural"] == again["events_checked"] == 0
    states = dict(connection.execute("SELECT document_id, events_status FROM news").fetchall())
    assert states[30] == states[31] == states[32] == "structural" and states[33] is None and states[34] is None
    assert connection.execute("SELECT COUNT(*) FROM company_events").fetchone()[0] == 3


def test_run_news_adds_structural_flag_and_web_shows_band_and_separate_prospects(hemply):
    from fastapi.testclient import TestClient

    from radar.web import create_app

    settings, connection, _ = hemply
    connection.close()
    client = EventClient()
    totals = news.run_news(settings, client, today=NOW, workers=1, log=lambda _line: None)
    assert totals["structural"] == 3 and totals["structural_change"] == 1 and totals["tokens_used"] >= 2700
    calls = client.calls
    news.run_news(settings, client, today=NOW, workers=1, log=lambda _line: None)
    assert client.calls == calls                                  # inget bedöms två gånger
    web = TestClient(create_app(settings))
    item = web.get("/api/companies").json()["items"][0]
    assert item["band"] == "structural" and "structural_change" in item["flags"]
    kinds = [(e["type"], e["status"]) for e in item["structural_events"]]
    assert ("reverse_acquisition", "completed") in kinds and ("name_change", "completed") in kinds
    assert len(kinds) == 2 and all(e["url"] and e["title"] for e in item["structural_events"])
    detail = web.get("/api/companies/1").json()
    assert "structural_change" in detail["flag_text"] and detail["structural_events"]
    prospects = web.get("/api/prospects").json()
    assert prospects["items"] == [] and prospects["raising"] == []
    assert [p["name"] for p in prospects["structural"]] == ["Exempel AB"] and "score" not in prospects["structural"][0]


def test_structural_check_covers_runway_up_to_18_months_without_financing_step(hemply):
    settings, connection, _ = hemply
    row = connection.execute("SELECT result_json FROM runway_results").fetchone()
    result = json.loads(row["result_json"]) | {"runway_today": 15.0, "flags": []}
    with connection:
        connection.execute("UPDATE runway_results SET result_json=?", (json.dumps(result),))
    assert news.news_targets(connection, 12.0)[0][4] is False
    assert news.news_targets(connection, 12.0, event_runway=10.0) == []
    assert news.news_targets(connection, 18.0)[0][4] is True
    exhausted = result | {"runway_today": 30.0, "flags": ["cash_exhausted_since_report"]}
    with connection:
        connection.execute("UPDATE runway_results SET result_json=?", (json.dumps(exhausted),))
    assert news.news_targets(connection, 12.0)[0][4] is True


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE news (document_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
            title TEXT NOT NULL, published_at TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT,
            text TEXT, tokens INTEGER NOT NULL DEFAULT 0, updated_at TEXT);""")
    connection = connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(news)")}
    assert {"events_status", "events_error", "events_tokens"} <= columns
    assert connection.execute("SELECT COUNT(*) FROM company_events").fetchone()[0] == 0


def test_frontend_has_structural_band_and_sections():
    from importlib import resources

    static = resources.files("radar") / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    for text in ("Strukturell förändring", "Företagshändelser efter rapporten", "runway bygger på äldre siffror",
                 "Kontrollera manuellt: rapportens siffror kan vara inaktuella", "Omvänt förvärv", "Avknoppning"):
        assert text in app, text
    assert "Strukturell förändring (kontrollera manuellt)" in html and 'id="structuralRows"' in html


def test_plain_acquisition_is_listed_but_does_not_move_the_company_to_the_structural_band():
    from radar.events import summarize

    listing, flags = summarize([ev("acquisition", "completed", "2026-11-02")], "2026-09-30")
    assert [e["type"] for e in listing] == ["acquisition"] and flags == []


def test_public_offer_and_delisting_put_the_company_in_the_structural_band():
    from radar.web import band

    base = {"status": "ok", "cash_flow_positive": False, "runway_today": 5.0}
    assert band({**base, "flags": ["public_offer"]}) == "structural"
    assert band({**base, "flags": ["delisting"]}) == "structural"
    assert band({**base, "flags": []}) == "3_9"
