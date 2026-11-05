"""Steg 5: marknadsvärde (Nasdaq), kapitalbehov, kontroller, poäng och /api/prospects."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from radar import marketcap
from radar.db import connect
from radar.marketcap import MarketDataError, parse_number, parse_summary, find_share
from radar.qualify import capital_need, qualify
from radar.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = "2026-11-04"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def item(**over) -> dict:
    base = {"cash": 20e6, "monthly_burn": 3e6, "months_since_report": 2.0, "confirmed_financing": 0.0,
            "runway_today": 4.7, "flags": [], "cash_flow_positive": False}
    return {**base, **over}


def history(*rows) -> list[dict]:
    return [{"type": t, "status": s, "gross_amount": a, "net_amount": None, "currency": "SEK", "event_date": d}
            for t, s, a, d in rows]


# --- Nasdaq-tolkning -----------------------------------------------------------

def test_parse_number_handles_spaces_commas_and_decimals():
    assert parse_number("1 234 567 890") == 1_234_567_890
    assert parse_number("1,234,567,890") == 1_234_567_890
    assert parse_number("1 234 567") == 1_234_567
    assert parse_number("12,5") == 12.5
    assert parse_number("1.234,5") == 1234.5
    assert parse_number("") is None and parse_number("n/a") is None and parse_number(None) is None


def test_search_picks_the_shares_instrument_and_summary_gives_cap_and_sector():
    share = find_share(fixture("nasdaq_search.json"))
    assert share["orderbookId"] == "TX2540138" and share["currency"] == "SEK"
    assert find_share(fixture("nasdaq_empty.json")) is None
    assert parse_summary(fixture("nasdaq_summary.json")) == (5_863_792_929.0, "SEK", "Technology")
    with pytest.raises(MarketDataError):
        parse_summary({"data": None})
    with pytest.raises(MarketDataError):
        parse_summary({"data": {"summaryData": {"marketCap": {"value": ""}}}})


def test_refresh_stores_value_error_and_respects_cache(tmp_path):
    connection = connect(tmp_path / "r.db")
    calls, sleeps = [], []

    def fetch(url):
        calls.append(url)
        if "search" in url:
            return fixture("nasdaq_empty.json") if "SE0000NGM" in url else fixture("nasdaq_search.json")
        return fixture("nasdaq_summary.json")

    wanted = [(1, "SE0003917798"), (2, "SE0000NGM001"), (3, None)]
    counts = marketcap.refresh_many(connection, wanted, TODAY, fetch=fetch, sleep=sleeps.append)
    assert counts == {"fetched": 1, "failed": 2, "cached": 0}
    assert all(s >= 0.5 for s in sleeps) and len(sleeps) >= 3
    rows = {r["company_id"]: r for r in connection.execute("SELECT * FROM market_data")}
    assert rows[1]["market_cap"] == 5_863_792_929.0 and rows[1]["sector"] == "Technology" and rows[1]["error"] is None
    assert "Nasdaq" in rows[2]["error"] and rows[3]["error"] == "ISIN saknas"
    n = len(calls)
    again = marketcap.refresh_many(connection, wanted, TODAY, fetch=fetch, sleep=sleeps.append)
    assert again["cached"] == 3 and len(calls) == n
    forced = marketcap.refresh_many(connection, wanted[:1], TODAY, force=True, fetch=fetch, sleep=sleeps.append)
    assert forced["fetched"] == 1 and len(calls) == n + 2
    old = marketcap.refresh_many(connection, wanted[:1], "2026-11-20", fetch=fetch, sleep=sleeps.append)
    assert old["fetched"] == 1  # äldre än 7 dagar


# --- Kapitalbehov och kontroller -----------------------------------------------

def test_capital_need_math_includes_confirmed_financing_and_rounds():
    need, cash = capital_need(item())              # kassa 20 - 3*2 = 14; behov 36 - 14 = 22
    assert cash == pytest.approx(14e6) and need == pytest.approx(22e6)
    need, cash = capital_need(item(confirmed_financing=10e6))
    assert cash == pytest.approx(24e6) and need == pytest.approx(12e6)
    need, _ = capital_need(item(monthly_burn=2.33e6, months_since_report=0.0, cash=10e6))
    assert need == pytest.approx(18.0e6)           # 27,96 - 10 = 17,96 -> 18,0
    assert capital_need(item(cash=100e6, months_since_report=0.0))[0] == 0.0
    assert capital_need(item(cash_flow_positive=True))[0] is None


def statuses(q) -> dict:
    return {c.key: c.status for c in q.checks}


def test_share_thresholds():
    market = lambda cap: {"market_cap": cap, "currency": "SEK"}  # noqa: E731
    assert statuses(qualify(item(), market(100e6), []))["share"] == "pass"      # 22 %
    assert statuses(qualify(item(), market(70e6), []))["share"] == "warn"       # 31 %
    assert statuses(qualify(item(), market(40e6), []))["share"] == "fail"       # 55 %
    q = qualify(item(), None, [])
    assert statuses(q)["share"] == "warn" and statuses(q)["quality"] == "warn" and q.share is None
    assert q.market_cap is None and "okänt" in q.summary


def test_size_runway_raise_history_and_sector_checks():
    cap = {"market_cap": 500e6, "currency": "SEK"}
    assert statuses(qualify(item(), cap, []))["size"] == "pass"                 # 22 MSEK
    small = qualify(item(monthly_burn=1e6, cash=8e6, months_since_report=0.0), cap, [])
    assert small.capital_need == pytest.approx(4e6) and statuses(small)["size"] == "fail"
    assert statuses(qualify(item(monthly_burn=1e6, cash=6e6, months_since_report=0.0), cap, []))["size"] == "warn"
    assert statuses(qualify(item(monthly_burn=15e6, cash=0, months_since_report=0.0), cap, []))["size"] == "fail"
    assert statuses(qualify(item(runway_today=1.5), cap, []))["runway"] == "warn"
    assert statuses(qualify(item(runway_today=12), cap, []))["runway"] == "fail"
    assert statuses(qualify(item(flags=["raise_announced"]), cap, []))["no_raise"] == "fail"
    assert statuses(qualify(item(flags=["stale_report"]), cap, []))["quality"] == "warn"
    old = history(("directed_issue", "completed", 15e6, "2019-05-01"), ("loan", "completed", 5e6, "2026-01-01"))
    with_history = qualify(item(), cap, old)
    assert statuses(with_history)["history"] == "pass" and with_history.last_issue["date"] == "2019-05-01"
    assert statuses(qualify(item(), cap, history(("loan", "completed", 5e6, "2026-01-01"))))["history"] == "warn"
    sector = next(c for c in with_history.checks if c.key == "sector")
    assert sector.weight == 10 and sector.status == "warn" and "okänd" in sector.reason


def test_score_orders_better_prospects_first_and_stays_in_range():
    cap = {"market_cap": 200e6, "currency": "SEK", "sector": "Health Care"}
    best = qualify(item(), cap, history(("rights_issue", "completed", 30e6, "2025-03-01")))
    no_history = qualify(item(), cap, [])
    big_share = qualify(item(), {"market_cap": 40e6, "currency": "SEK", "sector": "Health Care"}, [])
    unknown = qualify(item(), None, [])
    worst = qualify(item(runway_today=14, flags=["raise_announced", "stale_report"], monthly_burn=0.2e6,
                        cash=0, months_since_report=0.0), None, [])
    assert best.score == 100 > no_history.score > unknown.score > big_share.score > worst.score >= 0
    assert "Poäng 100/100" in best.summary and "22 MSEK" in best.summary


# --- Webb ----------------------------------------------------------------------

def seed(connection, company_id, name, isin, result, cap=None):
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin, market) VALUES (?,?,?,?)",
                           (company_id, name, isin, "First North"))
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at, status) "
                           "VALUES (?,?,?,?, 'extracted')", (company_id * 10, company_id, "Delårsrapport", "2026-08-15"))
        connection.execute("INSERT INTO runway_results VALUES (?,?,?,?)",
                           (company_id, company_id * 10, TODAY, json.dumps(result)))
        if cap:
            marketcap.store(connection, company_id, isin, cap=cap, currency="SEK", sector="Health Care")


def ok(runway, burn=3e6, cash=20e6, flags=()):
    return {"status": "ok", "runway_today": runway, "runway_at_report": runway, "monthly_burn": burn,
            "cash": cash, "months_since_report": 2.0, "period_end": "2026-06-30", "flags": list(flags),
            "cash_flow_positive": False, "confirmed_financing": 0.0, "announced_financing": 0.0,
            "financing_events": []}


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from radar.web import create_app

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connection = connect(settings.db_path)
    seed(connection, 1, "Bra AB", "SE1", ok(4.7), cap=150e6)
    seed(connection, 2, "Okänt AB", "SE2", ok(5.0))
    seed(connection, 3, "Annonserat AB", "SE3", ok(5.0, flags=["raise_announced"]), cap=100e6)
    seed(connection, 4, "Lång AB", "SE4", ok(15.0, burn=1e6), cap=100e6)
    seed(connection, 5, "Akut AB", "SE5", ok(1.0, burn=3e6, cash=6e6), cap=30e6)
    with connection:
        connection.execute("INSERT INTO news(document_id, company_id, title) VALUES (900, 1, 'n')")
        connection.execute("INSERT INTO financing_events(news_document_id, company_id, type, status, gross_amount, "
                           "currency, event_date, created_at) VALUES (900,1,'rights_issue','completed',25000000,"
                           "'SEK','2020-03-01','x')")
    connection.close()
    return TestClient(create_app(settings))


def test_prospects_endpoint_ranks_and_filters(client):
    items = client.get("/api/prospects").json()["items"]
    assert [i["name"] for i in items] == ["Bra AB", "Okänt AB", "Akut AB"]  # ej annonserad, ej lång runway
    bra = items[0]
    assert bra["score"] == 100 and bra["capital_need"] == pytest.approx(22e6) and bra["market_cap"] == 150e6
    assert bra["share"] == pytest.approx(22 / 150) and bra["last_issue"]["date"] == "2020-03-01"
    assert {c["key"] for c in bra["checks"]} >= {"runway", "size", "share", "history", "sector"}
    assert items[1]["market_cap"] is None and items[1]["share"] is None
    assert items[1]["score"] > items[2]["score"] and "okänt" in items[1]["summary"]


def test_detail_has_qualification_and_ui_has_prospect_view(client):
    detail = client.get("/api/companies/1").json()
    assert detail["qualification"]["score"] == 100 and len(detail["qualification"]["checks"]) == 7
    html = client.get("/").text
    assert 'data-view="prospects"' in html and "prospectRows" in html and "app.js?v=" in html


def test_sector_rewards_life_science_and_tech():
    from radar.qualify import check_sector

    assert check_sector("Health Care").status == "pass"
    assert check_sector("Technology").status == "pass"
    assert check_sector("Industrials").status == "warn"
