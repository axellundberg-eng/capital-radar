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


def test_much_faster_recent_quarter_is_used():
    result = compute(facts(("cash", 12e6, None), ("operating_cash_flow", -6e6, "2026-01-01"),
                           ("operating_cash_flow", -6e6, "2026-04-01")), END, today=END)
    assert result.monthly_burn == 2e6
    assert "recent_period_burn_higher" in result.flags


def test_negative_quarter_never_overrides_positive_half_year():
    result = compute(facts(("cash", 1e6, None), ("operating_cash_flow", 3e6, "2026-01-01"),
                           ("operating_cash_flow", -1e6, "2026-04-01")), END, today=END)
    assert result.cash_flow_positive


def test_capex_or_investing_rules():
    partial = compute(facts(("cash", 1e7, None), ("operating_cash_flow", 0, "2026-01-01"),
                            ("capex", -3e6, "2026-01-01"), ("investing_cash_flow", -6e6, "2026-01-01")),
                      END, today=END)
    assert partial.monthly_burn == 1e6 and "investing_as_capex" in partial.flags
    acquisition = compute(facts(("cash", 1e7, None), ("operating_cash_flow", 0, "2026-01-01"),
                                ("capex", 6e5, "2026-01-01"), ("investing_cash_flow", -6e7, "2026-01-01")),
                          END, today=END)
    assert acquisition.monthly_burn == 1e5


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
                                    profile_category TEXT);
            CREATE TABLE attachments (id INTEGER PRIMARY KEY, document_id INT, url TEXT);
            INSERT INTO issuers VALUES (1, 'Exempel AB');
            INSERT INTO securities VALUES (1, 1, 'SE0000000001', 'EX', 'First North', 'Growth', 1);
            INSERT INTO documents VALUES (10, 1, 'Delårsrapport Q2', '2026-08-20', 'report');
            INSERT INTO documents VALUES (11, 1, 'Pressmeddelande', '2026-08-21', 'other');
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
    assert import_legacy(connection, legacy_db) == {"companies": 1, "reports": 1, "reports_with_pdf": 1}

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
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde"])
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
        db.execute("INSERT INTO documents VALUES (9, 1, 'Delårsrapport Q1', '2026-05-20', 'report')")
        db.execute("INSERT INTO attachments VALUES (2, 9, 'https://storage.mfn.se/x/q1.pdf')")
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files")
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    assert batch.latest_reports(connection) == [(10, "Exempel AB")]
    connection.close()

    monkeypatch.setattr(pipeline, "fetch_pdf", lambda url, target, **_: (target, "abc"))
    monkeypatch.setattr(pipeline, "pdf_pages", lambda path: ["Kassaflöde"])
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
