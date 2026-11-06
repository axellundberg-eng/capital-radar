"""Burn på rullande tolv månader, leasing i burn och finansiering i annan valuta."""

from __future__ import annotations

import pytest

from radar import fx
from radar.db import connect
from radar.rounds import Event, summarize
from radar.runway import Fact, compute

END = "2026-06-30"
TODAY = "2026-06-30"


def flow(kind, value, start, end=END):
    return Fact(kind, value, start, end)


def test_ltm_burn_uses_comparatives_and_flags_recent_burn():
    # Halvåret +4 (engångsinbetalning i Q1), Q2 -4,6, föregående halvår -6, föregående helår -17.
    facts = [Fact("cash", 9.5e6, None, END), flow("operating_cash_flow", 4.0e6, "2026-01-01"),
             flow("operating_cash_flow", -4.6e6, "2026-04-01"),
             flow("operating_cash_flow", -6.0e6, "2025-01-01", "2025-06-30"),
             flow("operating_cash_flow", -17.0e6, "2025-01-01", "2025-12-31")]
    result = compute(facts, END, today=TODAY)
    assert result.burn_basis == "ltm" and not result.cash_flow_positive
    assert result.monthly_burn == pytest.approx(7.0e6 / 12)        # -17 + 4 - (-6) = -7 på tolv månader
    # Utan jämförelsekolumner: längsta perioden, men märkt att senaste perioden bränner.
    fallback = compute(facts[:3], END, today=TODAY)
    assert fallback.burn_basis == "longest_period" and fallback.cash_flow_positive
    assert "recent_period_burning" in fallback.flags


def test_lease_repayment_is_part_of_burn():
    facts = [Fact("cash", 12e6, None, END), flow("operating_cash_flow", -3e6, "2026-01-01"),
             flow("lease_repayment", -0.6e6, "2026-01-01")]
    result = compute(facts, END, today=TODAY)
    assert result.monthly_burn == pytest.approx(0.6e6)              # (3 + 0,6) / 6
    assert result.periods[0].lease == pytest.approx(0.6e6)


def test_foreign_currency_event_is_converted_or_flagged():
    eur = Event("directed_issue", "completed", 1e6, 1e6, "EUR", "2026-08-01", "q", "t", "u")

    def rate(amount, src, dst, day):
        return amount * 11.0 if (src, dst) == ("EUR", "SEK") else None

    confirmed, _, listing, _ = summarize([eur], END, "SEK", fx=rate)
    assert confirmed == pytest.approx(11e6) and listing[0]["original_currency"] == "EUR"
    confirmed, _, listing, _ = summarize([eur], END, "SEK")          # ingen kurs: räknas inte
    assert confirmed == 0 and listing[0]["unconverted"]
    facts = [Fact("cash", 12e6, None, END), flow("operating_cash_flow", -6e6, "2026-01-01")]
    flagged = compute(facts, END, today="2026-09-30", financing=summarize([eur], END, "SEK"))
    assert "unconverted_currency" in flagged.flags


def test_fx_refresh_stores_rates_and_never_guesses(tmp_path):
    connection = connect(tmp_path / "radar.db")

    def fake(series):
        if series == "SEKUSDPMI":
            raise OSError("429")
        return {"date": "2026-11-06", "value": 11.026}

    counts = fx.refresh(connection, {"EUR", "USD", "XYZ"}, fetch=fake, sleep=lambda s: None, today="2026-11-06")
    assert counts == {"fetched": 1, "fresh": 0, "unsupported": 1, "failed": 1}
    assert fx.latest_rates(connection) == {"SEK": 1.0, "EUR": 11.026}
    assert fx.refresh(connection, {"EUR"}, fetch=fake, sleep=lambda s: None, today="2026-11-06")["fresh"] == 1
    assert fx.convert(10, "EUR", "SEK", fx.latest_rates(connection)) == pytest.approx(110.26)
    assert fx.convert(10, "USD", "SEK", fx.latest_rates(connection)) is None
    assert fx.converter(connection)(1.0, "EUR", "SEK", "2026-12-01") == pytest.approx(11.026)


def test_set_off_issue_is_no_cash_and_part_of_the_rights_issue(tmp_path):
    # SciBase: företrädesemission 57,5 MSEK, kostnader 4,8 MSEK, kvittningsemission 0,9 MSEK till garanten.
    from radar.db import connect
    from radar.financing import load_events

    rights = Event("rights_issue", "completed", 57.5e6, None, "SEK", "2026-10-01", "utfall", "t", "u",
                   issue_costs=4.8e6)
    set_off = Event("set_off", "completed", 0.9e6, None, "SEK", "2026-10-07", "kvittning", "t", "u")
    confirmed, _, listing, _ = summarize([rights, set_off], END, "SEK")
    assert confirmed == pytest.approx(52.7e6)                       # 57,5 - 4,8; kvittningen ger ingen kassa
    assert len(listing) == 1 and listing[0]["paid_in_shares"] == pytest.approx(0.9e6)

    # Äldre extraktion kallade kvittningen "riktad emission": typen rättas vid läsning.
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'SciBase')")
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url, status) VALUES "
                           "(5, 1, 'SciBase beslutar om kvittningsemission av aktier till garant', '2026-10-07', 'u', 'financing')")
        connection.execute("INSERT INTO financing_events(news_document_id, company_id, type, status, gross_amount, "
                           "currency, event_date, quote, created_at) VALUES (5, 1, 'directed_issue', 'completed', 900000, "
                           "'SEK', '2026-10-07', 'betalning genom kvittning', '2026-10-07')")
    assert [e.type for e in load_events(connection, 1)] == ["set_off"]
