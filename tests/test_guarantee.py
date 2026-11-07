"""Garantiunderlag: kapacitetsbedömning samt /api/search och /api/guarantee (tillfällig databas, inget nätverk)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from radar import guarantee, insiders, market
from radar.db import connect
from radar.settings import Settings
from radar.web import create_app


@pytest.mark.parametrize("share, days, expected", [
    (10.0, 20.0, "lätt"), (15.0, 30.0, "lätt"),
    (15.1, 20.0, "möjlig"), (10.0, 31.0, "möjlig"), (40.0, 120.0, "möjlig"),
    (40.1, 10.0, "svår"), (10.0, 121.0, "svår"),
    (None, None, "okänd"), (10.0, None, "okänd"), (None, 10.0, "okänd"),
    (60.0, None, "svår"),   # redan över gränsen: saknad omsättning kan inte göra det lättare
])
def test_assess_rules(share, days, expected):
    verdict, reasons = guarantee.assess(share, days)
    assert verdict == expected and reasons and all(isinstance(r, str) for r in reasons)


def test_capacity_without_need_is_unknown():
    block = guarantee.capacity_block(None, {"market_cap": 1e8, "avg_daily_turnover_3m": 1e6})
    assert block["assessment"] == "okänd" and block["need_share_of_market_cap_pct"] is None


RESULT = {"status": "ok", "runway_today": 4.0, "monthly_burn": 5e6, "cash": 20e6, "cash_today": 20e6,
          "currency": "SEK", "flags": ["raise_announced"], "months_since_report": 1.0, "period_end": "2026-09-30"}


@pytest.fixture()
def settings(tmp_path):
    return replace(Settings.from_env(), db_path=tmp_path / "radar.db")


@pytest.fixture()
def client(settings):
    connection = connect(settings.db_path)
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin, symbol, market) VALUES "
                           "(1,'Scibase Holding','SE0001','SCIB','Nasdaq Stockholm'),"
                           "(2,'Annat Bolag','SE0002','ANN','Spotlight')")
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at) VALUES "
                           "(1,1,'Q3','2026-10-20')")
        connection.execute("INSERT INTO runway_results VALUES (1,1,'x',?)", (json.dumps(RESULT),))
        connection.execute("INSERT INTO market_data(company_id, isin, market_cap, currency, source, fetched_at, sector)"
                           " VALUES (1,'SE0001',200000000,'SEK','nasdaq','2026-11-01T00:00:00','Health Care')")
        connection.execute("INSERT INTO calendar_events(company_id, source_url, title, event_date, kind, created_at)"
                           " VALUES (1,'u','Delårsrapport Q4','2027-02-15','report','x')")
    market.store(connection, 1, "SE0001", order_book="TX1", currency="SEK", summary={
        "price": 5.0, "price_change_1m_pct": -3.2, "price_change_3m_pct": 1.0, "price_change_12m_pct": -40.0,
        "avg_daily_turnover_3m": 1_500_000, "avg_daily_volume_3m": 300_000, "high_52w": 9.0, "low_52w": 3.0,
        "as_of": "2026-11-06"})
    insiders.store_trades(connection, [
        {"company_id": 1, "published": "2026-10-20 09:00:00", "transaction_date": "2026-10-19", "person": "A",
         "role": "VD", "transaction_type": "Förvärv", "volume": 1000.0, "price": 5.0, "currency": "SEK",
         "value": 5000.0, "instrument": "Aktie", "source_key": "k1"}])
    connection.close()
    return TestClient(create_app(settings))


def test_search_matches_name_symbol_and_isin_case_insensitively(client):
    hits = client.get("/api/search", params={"q": "scib"}).json()
    assert [h["id"] for h in hits] == [1]
    assert hits[0].keys() >= {"id", "name", "symbol", "isin", "market", "band", "runway_today"}
    assert hits[0]["runway_today"] == 4.0 and hits[0]["band"] == "raising"
    assert [h["id"] for h in client.get("/api/search", params={"q": "HOLDING"}).json()] == [1]
    assert [h["id"] for h in client.get("/api/search", params={"q": "se0002"}).json()] == [2]
    other = client.get("/api/search", params={"q": "annat"}).json()[0]
    assert other["band"] == "missing" and other["runway_today"] is None
    assert client.get("/api/search", params={"q": ""}).json() == []
    assert len(client.get("/api/search", params={"q": "s", "limit": 1}).json()) == 1
    assert client.get("/api/search", params={"q": "scib"}).headers["cache-control"] == "no-store"


def test_guarantee_payload_follows_the_contract(client):
    assert client.get("/api/guarantee/999").status_code == 404
    data = client.get("/api/guarantee/1").json()
    assert data["company"]["name"] == "Scibase Holding" and data["company"]["sector"] == "Health Care"
    assert data["runway"]["runway_today"] == 4.0 and data["runway"]["band"] == "raising"
    assert data["runway"]["flag_text"]["raise_announced"]
    assert data["need"]["gross_need"] == 45.5e6                       # (5 MSEK x 12 - 20 MSEK) / 0.88, avrundat
    assert data["market"]["market_cap"] == 200e6 and data["market"]["price"] == 5.0
    cap = data["capacity"]
    assert cap["need_share_of_market_cap_pct"] == 22.8 and cap["turnover_days_for_need"] == 30.3
    assert cap["assessment"] == "möjlig" and cap["reasons"]
    assert data["insiders"][0]["person"] == "A" and data["insider_summary"]["buys"] == 5000
    assert data["next_report"] == {"date": "2027-02-15", "title": "Delårsrapport Q4"}
    assert data["authorization"] is None and data["emissions_24m"] == 0 and data["financing"] == []
    assert data["updated_at"]


def test_guarantee_for_company_without_data_has_nulls_not_invented_numbers(client):
    data = client.get("/api/guarantee/2").json()
    assert data["runway"]["band"] == "missing" and data["runway"]["runway_today"] is None
    assert data["need"] is None and data["market"]["price"] is None and data["market"]["market_cap"] is None
    assert data["capacity"]["assessment"] == "okänd" and data["insiders"] == []
    assert data["insider_summary"]["buys"] == 0 and data["next_report"] is None
