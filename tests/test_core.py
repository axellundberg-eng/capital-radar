from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from radar import pipeline
from radar.db import connect
from radar.extract import ExtractionError, parse_response, select_pages
from radar.importer import import_legacy
from radar.runway import Fact, compute
from radar.settings import Settings

END = "2026-06-30"


def facts(*items):
    return [Fact(kind, value, start, END) for kind, value, start in items]


# --- Runway-regler -------------------------------------------------------------

def test_longest_period_is_used_and_cash_rolls_forward():
    result = compute(facts(("cash", 12e6, None), ("operating_cash_flow", -6e6, "2026-01-01"),
                           ("operating_cash_flow", -4e6, "2026-04-01")), END, today="2026-09-30")
    assert result.monthly_burn == 1e6
    assert result.runway_at_report == 12.0
    assert result.months_since_report == 3.0
    assert result.runway_today == 9.0


def test_much_faster_recent_quarter_is_shown_beside_the_main_number():
    result = compute(facts(("cash", 12e6, None), ("operating_cash_flow", -6e6, "2026-01-01"),
                           ("operating_cash_flow", -6e6, "2026-04-01")), END, today=END)
    assert result.monthly_burn == 1e6 and result.runway_today == 12.0
    assert result.recent_monthly_burn == 2e6 and result.recent_runway_today == 6.0
    assert "recent_period_burn_higher" in result.flags


def test_negative_quarter_never_overrides_positive_half_year():
    result = compute(facts(("cash", 1e6, None), ("operating_cash_flow", 3e6, "2026-01-01"),
                           ("operating_cash_flow", -1e6, "2026-04-01")), END, today=END)
    assert result.cash_flow_positive
    assert "recent_period_burn_higher" in result.flags


def test_investment_priority_excludes_acquisitions():
    # Rörelseinvesteringar (utan förvärv) går före capex och investeringsflöde.
    operating = compute(facts(("cash", 1e7, None), ("operating_cash_flow", 0, "2026-01-01"),
                              ("operating_investments", -3e6, "2026-01-01"), ("capex", -4e6, "2026-01-01"),
                              ("investing_cash_flow", -6e7, "2026-01-01")), END, today=END)
    assert operating.monthly_burn == 5e5
    capex_only = compute(facts(("cash", 1e7, None), ("operating_cash_flow", 0, "2026-01-01"),
                               ("capex", 6e5, "2026-01-01"), ("investing_cash_flow", -6e7, "2026-01-01")),
                         END, today=END)
    assert capex_only.monthly_burn == 1e5 and "investing_as_capex" not in capex_only.flags
    fallback = compute(facts(("cash", 1e7, None), ("operating_cash_flow", 0, "2026-01-01"),
                             ("investing_cash_flow", -6e6, "2026-01-01")), END, today=END)
    assert fallback.monthly_burn == 1e6 and "investing_as_capex" in fallback.flags


def test_missing_data_is_a_status():
    assert compute(facts(("operating_cash_flow", -1, "2026-01-01")), END, today=END).status == "missing_cash"
    assert compute(facts(("cash", 1, None)), END, today=END).status == "missing_cash_flow"


# --- Extraktion ----------------------------------------------------------------

def test_parse_response_scales_units_and_keeps_periods():
    raw = json.dumps({"report_period_start": "2026-01-01", "report_period_end": END, "unit": "TSEK",
                      "currency": "SEK", "cash": {"value": "12 500", "date": END, "page": 4, "quote": "Likvida medel"},
                      "cash_flows": [{"kind": "operating_cash_flow", "value": -3200, "period_start": "2026-04-01",
                                      "period_end": END, "page": 9, "quote": "Kassaflöde"},
                                     {"kind": "dividends", "value": 1}]})
    result = parse_response(raw)
    cash, cfo = result.facts
    assert (cash.kind, cash.value, cash.period_end) == ("cash", 12_500_000, END)
    assert (cfo.value, cfo.period_start) == (-3_200_000, "2026-04-01")


def test_parse_response_rejects_bad_answers():
    for raw in ("inte json", "[]", json.dumps({"unit": "TSEK"}), json.dumps({"report_period_end": "fel"})):
        with pytest.raises(ExtractionError):
            parse_response(raw)


def test_select_pages_prefers_cash_flow_pages():
    pages = ["Vd har ordet", "Kassaflödesanalys kassaflöde från den löpande verksamheten", "Styrelsen",
             "Balansräkning likvida medel"]
    assert select_pages(pages) == [2, 4]


# --- Import och kedja ----------------------------------------------------------

@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "screener.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE issuers (id INTEGER PRIMARY KEY, canonical_name TEXT);
            CREATE TABLE securities (id INTEGER PRIMARY KEY, issuer_id INT, isin TEXT, symbol TEXT,
                                     market TEXT, segment TEXT, active INT);
            CREATE TABLE documents (id INTEGER PRIMARY KEY, issuer_id INT, title TEXT, published_at TEXT,
                                    profile_category TEXT, canonical_url TEXT);
            CREATE TABLE attachments (id INTEGER PRIMARY KEY, document_id INT, url TEXT);
            INSERT INTO issuers VALUES (1, 'Exempel AB');
            INSERT INTO securities VALUES (1, 1, 'SE0000000001', 'EX', 'First North', 'Growth', 1);
            INSERT INTO documents VALUES (10, 1, 'Delårsrapport Q2', '2026-08-20', 'report', NULL);
            INSERT INTO documents VALUES (11, 1, 'Pressmeddelande', '2026-08-21', 'other', 'https://mfn.se/a/11');
            INSERT INTO attachments VALUES (1, 10, 'https://storage.mfn.se/x/rapport.pdf');
        """)
    return path


class FakeClient:
    def __init__(self, answer: dict) -> None:
        self.answer = answer

    def complete(self, system, user):
        assert "Kassaflöde" in user
        return json.dumps(self.answer), {"prompt_tokens": 100, "completion_tokens": 50}


def test_import_then_pipeline_end_to_end(tmp_path, legacy_db, monkeypatch):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files",
                       legacy_db_path=legacy_db)
    connection = connect(settings.db_path)
    assert import_legacy(connection, legacy_db) == {"companies": 1, "reports": 1, "reports_with_pdf": 1, "news": 1,
                                                       "mfn_slugs": 0}

    def fake_fetch(url, target, **_):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4")
        return target, "abc"

    monkeypatch.setattr(pipeline, "fetch_pdf", fake_fetch)
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Omslag", "Kassaflöde från den löpande verksamheten"])
    answer = {"report_period_start": "2026-01-01", "report_period_end": END, "unit": "MSEK", "currency": "SEK",
              "cash": {"value": 12, "date": END},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END}]}
    outcome = pipeline.process(connection, settings, FakeClient(answer), 10, today="2026-09-30")
    assert outcome["runway"]["monthly_burn"] == 1e6
    assert outcome["runway"]["runway_today"] == 9.0
    status = connection.execute("SELECT status, page_count FROM reports WHERE document_id=10").fetchone()
    assert tuple(status) == ("extracted", 2)
    assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_pipeline_failure_is_recorded_and_retried(tmp_path, legacy_db, monkeypatch):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)

    def broken(*_, **__):
        raise OSError("timeout")

    monkeypatch.setattr(pipeline, "fetch_pdf", broken)
    outcome = pipeline.process(connection, settings, None, 10, today=END)
    assert outcome["status"] == "failed" and "hämta" in outcome["error"]
    row = connection.execute("SELECT status, error FROM reports WHERE document_id=10").fetchone()
    assert row["status"] == "failed" and "timeout" in row["error"]


def test_web_lists_companies_by_band_and_explains_detail(tmp_path, legacy_db, monkeypatch):
    from fastapi.testclient import TestClient

    from radar.web import create_app

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde", "Balansräkning likvida medel 12"])
    answer = {"report_period_start": "2026-01-01", "report_period_end": END, "unit": "MSEK",
              "cash": {"value": 12, "date": END, "page": 1, "quote": "Likvida medel 12"},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END, "page": 1}]}
    pipeline.process(connection, settings, FakeClient(answer), 10, today="2026-12-31")
    client = TestClient(create_app(settings))
    overview = client.get("/api/overview").json()
    assert overview["with_result"] == 1
    items = client.get("/api/companies").json()["items"]
    assert items[0]["name"] == "Exempel AB" and items[0]["runway_at_report"] == 12.0
    detail = client.get("/api/companies/1").json()
    assert detail["facts"][0]["quote"] == "Likvida medel 12"
    assert detail["pdf_url"].endswith("rapport.pdf")
    assert client.get("/api/companies/999").status_code == 404
    assert "Capital Radar" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_facit_line_handles_missing_values(capsys, monkeypatch, tmp_path):
    import radar.__main__ as cli

    facit = tmp_path / "facit.json"
    facit.write_text(json.dumps([{"company_id": 1, "name": "X", "document_id": 1,
                                  "expected_monthly_burn": None, "cash_flow_positive": None}]), encoding="utf-8")
    monkeypatch.setattr(cli, "FACIT", facit)
    monkeypatch.setattr(cli, "process", lambda *a, **k: {"runway": {"status": "missing_cash"}})
    cli.run_facit(None, Settings.from_env(), None, END)
    assert "saknas" in capsys.readouterr().out


def test_batch_takes_latest_report_per_company_and_respects_budget(tmp_path, legacy_db, monkeypatch):
    from radar import batch

    with sqlite3.connect(legacy_db) as db:
        db.execute("INSERT INTO documents VALUES (9, 1, 'Delårsrapport Q1', '2026-05-20', 'report', NULL)")
        db.execute("INSERT INTO attachments VALUES (2, 9, 'https://storage.mfn.se/x/q1.pdf')")
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    assert batch.latest_reports(connection) == [(10, "Exempel AB")]
    connection.close()

    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde", "Balansräkning likvida medel 12"])
    calls = []

    class CountingClient(FakeClient):
        def complete(self, system, user):
            calls.append(1)
            return super().complete(system, user)

    answer = {"report_period_end": END, "unit": "MSEK", "cash": {"value": 12, "date": END},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END}]}
    lines = []
    exhausted = batch.run_latest(settings, CountingClient(answer), today=END, token_budget=0, log=lines.append)
    assert calls == [] and exhausted.get("budget_slut") == 1
    done = batch.run_latest(settings, CountingClient(answer), today=END, log=lines.append)
    assert calls == [1] and done.get("ok") == 1
    again = batch.run_latest(settings, CountingClient(answer), today=END, log=lines.append)
    assert calls == [1] and again.get("ok") == 1  # redan extraherad: inget nytt AI-anrop


def test_parse_response_accepts_fenced_or_wrapped_json():
    body = json.dumps({"report_period_end": END, "unit": "SEK", "cash": {"value": 5, "date": END}})
    for raw in (f"```json\n{body}\n```", f"Här är svaret:\n{body}\nKlart."):
        assert parse_response(raw).facts[0].value == 5


def test_english_unit_suffixes():
    from radar.units import parse_unit

    assert parse_unit("SEKm") == (1e6, "SEK")
    assert parse_unit("SEK m") == (1e6, "SEK")
    assert parse_unit("EURk") == (1e3, "EUR")
    assert parse_unit("SEKbn") == (1e9, "SEK")


def test_extract_retries_once_on_broken_answer():
    from radar.extract import extract

    good = json.dumps({"report_period_end": END, "unit": "SEK", "cash": {"value": 5, "date": END}})
    answers = iter(['{"report_period_end": "2026', good])

    class Flaky:
        def complete(self, system, user):
            return next(answers), {"prompt_tokens": 10, "completion_tokens": 5}

    result, _, _, usage = extract(Flaky(), ["Kassaflöde"])
    assert result.facts[0].value == 5 and usage["prompt_tokens"] == 20


def test_short_term_investments_count_as_liquidity_and_exhausted_cash_is_flagged():
    base = [("cash", 1e6, None), ("short_term_investments", 2e6, None), ("operating_cash_flow", -6e6, "2026-01-01")]
    result = compute(facts(*base), END, today="2026-08-30")
    assert result.cash == 3e6 and "includes_short_term_investments" in result.flags
    late = compute(facts(*base), END, today="2026-12-31")
    assert "cash_exhausted_since_report" in late.flags and late.runway_today == 0.0


def test_one_report_per_company_when_published_in_two_languages(tmp_path, legacy_db):
    from radar import batch

    with sqlite3.connect(legacy_db) as db:
        db.execute("INSERT INTO documents VALUES (12, 1, 'Interim report Q2', '2026-08-20', 'report', NULL)")
        db.execute("INSERT INTO attachments VALUES (3, 12, 'https://storage.mfn.se/x/q2-en.pdf')")
    connection = connect(tmp_path / "radar.db")
    import_legacy(connection, legacy_db)
    assert batch.latest_reports(connection) == [(10, "Exempel AB")]


def test_token_budget_counts_only_this_runs_own_reports(tmp_path, legacy_db, monkeypatch):
    from radar import batch

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    with connection:  # tidigare körningars tokens får inte räknas mot den här budgeten
        connection.execute("INSERT INTO extractions(document_id, model, pages, status, prompt_tokens, "
                           "completion_tokens, created_at) VALUES (10, 'm', '[]', 'invalid', 900000, 0, 'x')")
    connection.close()
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde", "Balansräkning likvida medel 12"])
    answer = {"report_period_end": END, "unit": "MSEK", "cash": {"value": 12, "date": END},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END}]}
    counts = batch.run_latest(settings, FakeClient(answer), today=END, token_budget=1000, log=lambda _: None)
    assert counts.get("ok") == 1 and counts["tokens_used"] == 150


def test_balance_sheet_page_is_always_sent():
    pages = ["kassaflöde"] * 10 + ["Balansräkning Summa tillgångar 100"]
    assert 11 in select_pages(pages)


def test_short_release_falls_back_to_older_full_report(tmp_path, legacy_db, monkeypatch):
    from radar import batch

    with sqlite3.connect(legacy_db) as db:
        db.execute("INSERT INTO documents VALUES (13, 1, 'Kommuniké Q3', '2026-10-20', 'report', NULL)")
        db.execute("INSERT INTO attachments VALUES (4, 13, 'https://storage.mfn.se/x/kort.pdf')")
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    pages = {13: ["Rörelseresultatet ökade. Kassaflödet förbättrades."],
             10: ["Kassaflöde från den löpande verksamheten", "Balansräkning likvida medel 12"]}
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: pages[int(Path(path).stem)])
    calls = []

    class Recording(FakeClient):
        def complete(self, system, user):
            calls.append(user)
            return super().complete(system, user)

    answer = {"report_period_end": END, "unit": "MSEK", "cash": {"value": 12, "date": END},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END}]}
    outcome = batch.process_with_fallback(connection, settings, Recording(answer), 13, today=END)
    assert outcome["runway"]["status"] == "ok" and outcome["document_id"] == 10
    assert len(calls) == 1  # det korta dokumentet skickades aldrig till AI
    assert connection.execute("SELECT status FROM reports WHERE document_id=13").fetchone()[0] == "skipped"


def test_company_without_full_report_stays_visible_as_missing(tmp_path, legacy_db, monkeypatch):
    from fastapi.testclient import TestClient

    from radar import batch
    from radar.web import create_app

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Inbjudan till presentation"])
    outcome = batch.process_with_fallback(connection, settings, FakeClient({}), 10, today=END)
    assert outcome["runway"]["status"] == "missing_full_report"
    items = TestClient(create_app(settings)).get("/api/companies").json()["items"]
    assert [(i["name"], i["band"]) for i in items] == [("Exempel AB", "missing")]


# --- Händelser efter perioden och kortfristig skuld ----------------------------

XSPRAY = [("cash", 180e6, None), ("operating_cash_flow", -79.2e6, "2026-01-01")]


def test_post_period_outflow_is_subtracted_and_inflow_added():
    base = compute(facts(*XSPRAY), END, today="2026-09-30")
    result = compute(facts(*XSPRAY, ("post_period_outflow", 50e6, "2026-07-10"),
                           ("post_period_inflow", 30e6, "2026-08-01")), END, today="2026-09-30")
    assert round(base.runway_today - result.runway_today, 1) == 1.5     # netto 20 ut / 13,2 per mån
    assert result.post_period_outflows == 50e6 and result.reported_inflows_after_period == 30e6
    assert "post_period_outflows" in result.flags and "post_period_inflows" in result.flags
    assert result.runway_at_report == base.runway_at_report


def test_debt_due_12m_warns_without_changing_main_runway():
    base = compute(facts(*XSPRAY), END, today="2026-09-30")
    result = compute(facts(*XSPRAY, ("debt_due_12m", 60e6, None)), END, today="2026-09-30")
    assert result.runway_today == base.runway_today
    assert result.debt_due_12m == 60e6 and result.runway_after_debt == round((180e6 - 39.6e6 - 60e6) / 13.2e6, 1)
    assert "debt_due_within_12m" in result.flags
    small = compute(facts(*XSPRAY, ("debt_due_12m", 5e6, None)), END, today="2026-09-30")
    assert "debt_due_within_12m" not in small.flags


def test_parse_response_reads_events_after_period_and_debt_with_units():
    raw = json.dumps({"report_period_end": END, "unit": "MSEK", "currency": "SEK",
                      "cash": {"value": 180, "page": 4, "quote": "Likvida medel"},
                      "events_after_period": [
                          {"description": "Amortering", "direction": "out", "amount": 50, "date": "2026-07-10",
                           "page": 12, "quote": "amorterade 50 MSEK"},
                          {"description": "Lån", "direction": "in", "amount": 20, "date": None},
                          {"description": "Okänd", "direction": "sideways", "amount": 5}],
                      "debt_due_12m": {"value": 60, "page": 5, "quote": "Kortfristiga skulder"}})
    kinds = {f.kind: f for f in parse_response(raw).facts}
    out = kinds["post_period_outflow"]
    assert (out.value, out.period_start, out.page) == (50e6, "2026-07-10", 12) and "amorterade" in out.quote
    assert kinds["post_period_inflow"].value == 20e6 and kinds["debt_due_12m"].value == 60e6
    assert len(kinds) == 4


def test_schedule_cycle_imports_processes_and_records_changes(tmp_path, legacy_db, monkeypatch):
    from radar import scheduler
    from radar.web import latest_rows

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files",
                       legacy_db_path=legacy_db, collect_source="legacy")
    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde", "Balansräkning likvida medel 12"])
    monkeypatch.setattr(scheduler, "refresh_many", lambda *a, **k: {"fetched": 0})
    monkeypatch.setattr(scheduler, "run_news", lambda *a, **k: {"companies": 0})
    answer = {"report_period_end": END, "unit": "MSEK", "cash": {"value": 30, "date": END},
              "cash_flows": [{"kind": "operating_cash_flow", "value": -6, "period_start": "2026-01-01",
                              "period_end": END}]}
    first = scheduler.run_cycle(settings, FakeClient(answer), report_budget=10000, news_budget=0,
                                log=lambda _: None)
    assert first["reports"]["ok"] == 1 and first["changes"] == 0  # första varvet: inget att jämföra mot
    connection = connect(settings.db_path)
    with connection:  # låtsas att bolaget tidigare låg i ett annat band
        connection.execute("UPDATE company_status SET band='over_18'")
    item = latest_rows(connection)[0]
    assert scheduler.record_changes(connection, [item]) == (1 if item["band"] in scheduler.CHANGE_TEXT else 0)
    assert not (tmp_path / "radar.lock").exists()


def test_change_for_detects_new_raise_and_band_moves():
    from radar.scheduler import change_for

    previous = {"band": "9_18", "raise_announced": 0}
    assert change_for(previous, {"band": "3_9", "flags": []}) == "Ny i 3–9 mån"
    assert change_for(previous, {"band": "9_18", "flags": ["raise_announced"]}) == "Emission annonserad"
    assert change_for(previous, {"band": "9_18", "flags": []}) is None
    assert change_for(None, {"band": "3_9", "flags": []}) is None


def test_cycle_lock_takes_over_a_dead_lock_but_not_a_live_one(tmp_path):
    import os
    import time as _time

    from radar import scheduler

    path = tmp_path / "radar.lock"
    path.write_text("999")
    old = _time.time() - 2 * scheduler.STALE_LOCK_SECONDS
    os.utime(path, (old, old))  # övergivet: inget hjärtslag på länge
    with scheduler.CycleLock(path):
        assert path.exists()
        with pytest.raises(FileExistsError):  # levande lås: ett andra varv får inte starta
            with scheduler.CycleLock(path):
                pass
    assert not path.exists()


def test_cycle_lock_heartbeat_keeps_a_long_cycle_alive(tmp_path, monkeypatch):
    import os
    import time as _time

    from radar import scheduler

    monkeypatch.setattr(scheduler, "HEARTBEAT_SECONDS", 0.05)
    path = tmp_path / "radar.lock"
    with scheduler.CycleLock(path):
        old = _time.time() - 10
        os.utime(path, (old, old))
        _time.sleep(0.3)  # hjärtslaget ska ha uppdaterat ändringstiden
        assert _time.time() - path.stat().st_mtime < 5
