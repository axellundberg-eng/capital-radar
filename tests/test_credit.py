"""Kapitallösningar: brygglån, kreditfacilitet, kreditreserv, akut runway och kategorin pågående anskaffning."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from radar.db import connect
from radar.extract import parse_response
from radar.financing import looks_like_financing, parse_financing
from radar.rounds import Event, dedupe, summarize
from radar.runway import Fact, compute
from radar.settings import Settings
from radar.web import band

END = "2026-06-30"
TODAY = "2026-09-30"


def event(kind, status="completed", gross=10e6, drawn=None, day="2026-08-01", net=None):
    return Event(kind, status, gross, net, "SEK", day, "citat", "Rubrik", "https://mfn.se/a/1", drawn)


def facts(cash=12e6, cfo=-6e6, *extra):
    return [Fact("cash", cash, None, END), Fact("operating_cash_flow", cfo, "2026-01-01", END), *extra]


def test_parse_new_types_and_drawn_amount():
    raw = json.dumps({"is_financing": True, "events": [
        {"type": "bridge_loan", "status": "completed", "gross_amount": 10, "unit": "MSEK", "currency": "SEK",
         "date": "2026-09-01", "quote": "brygglån"},
        {"type": "credit_facility", "status": "completed", "gross_amount": 30, "drawn_amount": 5,
         "unit": "MSEK", "currency": "SEK", "date": "2026-09-02", "quote": "kreditram"}]})
    bridge, facility = parse_financing(raw, "2026-09-05")
    assert bridge.type == "bridge_loan" and bridge.gross_amount == 10e6 and bridge.drawn_amount is None
    assert facility.type == "credit_facility" and facility.gross_amount == 30e6 and facility.drawn_amount == 5e6
    assert looks_like_financing("Bolaget erhåller checkkredit") and looks_like_financing("Ny kreditlina om 20 MSEK")
    assert looks_like_financing("Revolving credit line signed")


def test_bridge_loan_counts_gross_like_a_loan():
    confirmed, announced, listing, credit = summarize([event("bridge_loan", gross=8e6)], END, "SEK")
    assert confirmed == 8e6 and credit == 0.0 and listing[0]["type"] == "bridge_loan"


def test_credit_facility_is_reserve_not_cash():
    confirmed, _, listing, credit = summarize([event("credit_facility", gross=30e6)], END, "SEK")
    assert confirmed == 0.0 and credit == 30e6
    confirmed, _, _, credit = summarize([event("credit_facility", gross=30e6, drawn=5e6)], END, "SEK")
    assert confirmed == 5e6 and credit == 25e6
    _, announced, _, credit = summarize([event("credit_facility", "announced", gross=30e6)], END, "SEK")
    assert announced == 0.0 and credit == 0.0
    assert len(dedupe([event("credit_facility"), event("loan")])) == 2


def test_undrawn_credit_fact_is_parsed():
    raw = json.dumps({"report_period_end": END, "unit": "MSEK", "currency": "SEK",
                      "cash": {"value": 12, "date": END},
                      "undrawn_credit": {"value": 20, "page": 7, "quote": "outnyttjad checkkredit 20"}})
    fact = next(f for f in parse_response(raw).facts if f.kind == "undrawn_credit")
    assert fact.value == 20e6 and fact.period_end == END and fact.page == 7


def test_runway_today_includes_credit_and_cash_runway_is_separate():
    base = compute(facts(), END, today=TODAY)
    reserve = compute(facts(12e6, -6e6, Fact("undrawn_credit", 9e6, None, END)), END, today=TODAY)
    assert reserve.runway_cash_today == base.runway_cash_today == 9.0
    assert reserve.runway_today == 18.0 and reserve.available_credit == 9e6
    assert "credit_reserve" in reserve.flags and base.runway_with_credit is None
    news = summarize([event("credit_facility", gross=9e6)], END, "SEK")
    from_news = compute(facts(), END, today=TODAY, financing=news)
    assert from_news.runway_with_credit == 18.0 and from_news.confirmed_financing == 0.0
    assert from_news.runway_cash_today == 9.0


def test_acute_flag_and_band():
    acute = compute(facts(cash=1.5e6), END, today=TODAY)   # burn 1 MSEK/mån, 3 mån gått: precis slut
    low = compute(facts(cash=3.5e6), END, today=TODAY)     # 0,5 mån kvar
    assert "acute" not in acute.flags or acute.runway_today < 1
    assert low.runway_today == 0.5 and "acute" in low.flags
    assert band({**low.to_dict()}) == "acute"
    done = summarize([event("rights_issue", gross=1.2e6, net=1.1e6)], END, "SEK")
    refinanced = compute(facts(cash=3.5e6), END, today=TODAY, financing=done)
    assert "acute" not in refinanced.flags
    assert "acute" not in compute(facts(), END, today=TODAY).flags


def test_raising_band_precedence():
    ok = {"status": "ok", "cash_flow_positive": False, "runway_today": 5.0, "flags": ["raise_announced"]}
    assert band(ok) == "raising"
    assert band({**ok, "runway_today": 0.0, "flags": ["raise_announced", "cash_exhausted_since_report", "acute"]}) == "raising"
    assert band({**ok, "cash_flow_positive": True}) == "positive"
    assert band({**ok, "status": "missing_cash"}) == "missing"
    assert band({**ok, "flags": ["acute"], "runway_today": 0.5}) == "acute"
    assert band({**ok, "flags": []}) == "3_9"
    # återbetalning ger inte kategorin; all annonserad tillförsel (även lån och kreditfacilitet) väntar på utfall
    pending = summarize([event("repayment", "announced")], END, "SEK")
    assert "raise_announced" not in compute(facts(), END, today=TODAY, financing=pending).flags
    for kind in ("credit_facility", "loan", "bridge_loan", "rights_issue", "convertible"):
        pending = summarize([event(kind, "announced")], END, "SEK")
        assert "raise_announced" in compute(facts(), END, today=TODAY, financing=pending).flags


def test_ui_has_no_hide_checkbox_and_prospects_have_raising_section(tmp_path):
    from fastapi.testclient import TestClient

    from radar.web import create_app
    from test_qualify import ok, seed

    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connection = connect(settings.db_path)
    seed(connection, 1, "Anskaffar AB", "SE1", ok(5.0, flags=["raise_announced"]), cap=100e6)
    seed(connection, 2, "Akut AB", "SE2", ok(0.5, flags=["acute"], cash=1.5e6), cap=40e6)
    connection.close()
    client = TestClient(create_app(settings))
    html = client.get("/").text
    assert "hideRaise" not in html and "Dölj bolag med annonserad emission" not in html
    assert "raisingRows" in html and "för sent för garanti" in html
    by_name = {i["name"]: i for i in client.get("/api/companies").json()["items"]}
    assert by_name["Anskaffar AB"]["band"] == "raising" and by_name["Akut AB"]["band"] == "acute"
    data = client.get("/api/prospects").json()
    assert [i["name"] for i in data["raising"]] == ["Anskaffar AB"]
    assert [i["name"] for i in data["items"]] == ["Akut AB"]
    assert any(c["key"] == "runway" and c["status"] == "warn" for c in data["items"][0]["checks"])
    js = client.get("/static/app.js").text
    assert "Pågående kapitalanskaffning" in js and "Akut &lt; 1 mån" not in js and "Brygglån" in js
