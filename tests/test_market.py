"""Kurshistorik i sammandrag (Nasdaq-chart): tolkning, beräkning och lagring, utan nätverk."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from radar import market, marketcap
from radar.db import connect

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = "2026-11-07"


def make_points(days=380, last=date(2026, 11, 6), start_price=10.0, step=0.01, volume=100_000.0):
    """Syntetiska dagspunkter, en per kalenderdag bakåt från `last`; stängning stiger lite per dag."""
    points = []
    for i in range(days):
        day = last - timedelta(days=days - 1 - i)
        close = round(start_price + step * i, 4)
        points.append({"date": day.isoformat(), "close": close, "high": close + 0.5, "low": close - 0.5,
                       "volume": volume})
    return points


def test_parse_chart_reads_daily_points_and_skips_intraday():
    payload = json.loads((FIXTURES / "nasdaq_chart.json").read_text(encoding="utf-8"))
    points = market.parse_chart(payload)
    assert points[0]["date"] == "2025-11-03" and points[-1]["date"] == "2026-11-06"
    assert points[0]["close"] == 3.664 and points[0]["volume"] == 1318179.0 and points[0]["high"] == 3.778
    intraday = {"data": {"CP": [{"z": {"dateTime": "09:01 CET", "value": "14.80"}, "x": 1, "y": 14.8}]}}
    assert market.parse_chart(intraday) == []


def test_summary_computes_changes_turnover_and_range():
    points = make_points()
    summary = market.summarize(points)
    last = points[-1]["close"]
    assert summary["price"] == last and summary["as_of"] == "2026-11-06"
    one_month_ago = next(p["close"] for p in points if p["date"] == "2026-10-06")
    assert summary["price_change_1m_pct"] == round(100 * (last / one_month_ago - 1), 2)
    assert summary["price_change_12m_pct"] is not None and summary["price_change_3m_pct"] is not None
    assert summary["avg_daily_volume_3m"] == 100_000
    assert summary["avg_daily_turnover_3m"] > 0
    assert summary["high_52w"] == pytest.approx(last + 0.5)
    assert summary["low_52w"] < last - 3


def test_summary_never_invents_missing_history():
    short = market.summarize(make_points(days=45))
    assert short["price"] is not None and short["price_change_1m_pct"] is not None
    assert short["price_change_3m_pct"] is None and short["price_change_12m_pct"] is None
    assert short["high_52w"] is None                                              # historiken täcker inte året
    assert market.summarize(make_points(days=15))["avg_daily_turnover_3m"] is None  # för få handelsdagar
    with pytest.raises(marketcap.MarketDataError):
        market.summarize([])


def test_foreign_currency_needs_a_rate_for_turnover():
    assert market.summarize(make_points(), None)["avg_daily_turnover_3m"] is None
    assert market.summarize(make_points(), 11.0)["avg_daily_turnover_3m"] == pytest.approx(
        11 * market.summarize(make_points(), 1.0)["avg_daily_turnover_3m"], rel=1e-6)


@pytest.fixture()
def connection(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin) VALUES (1,'Foo AB','SE0001'),"
                           "(2,'Bar AB','SE0002'),(3,'Utan ISIN',NULL)")
        for company_id in (1, 2, 3):
            connection.execute("INSERT INTO reports(document_id, company_id, title) VALUES (?,?,'Q3')",
                               (company_id, company_id))
            connection.execute("INSERT INTO runway_results VALUES (?,?,?,?)", (company_id, company_id, "x", "{}"))
    return connection


def fake_fetch(calls):
    chart = {"data": {"CP": [{"z": {"dateTime": p["date"], "close": str(p["close"]), "high": str(p["high"]),
                                    "low": str(p["low"]), "volume": f"{int(p['volume']):,}"}}
                             for p in make_points()]}}

    def fetch(url):
        calls.append(url)
        if "search" in url:
            if "SE0002" in url:
                return {"data": None}
            return {"data": [{"instruments": [{"orderbookId": "TX1", "assetClass": "SHARES", "currency": "SEK"}]}]}
        return chart
    return fetch


def test_run_stores_summary_and_errors_and_fetches_once_per_day(connection):
    calls: list[str] = []
    sleeps: list[float] = []
    counts = market.run(connection, None, fetch=fake_fetch(calls), sleep=sleeps.append, today=TODAY,
                        log=lambda _: None)
    assert counts["fetched"] == 1 and counts["failed"] == 1   # bolag 3 saknar ISIN och ingår inte
    ok = market.load(connection, 1)
    assert ok["error"] is None and ok["order_book"] == "TX1" and ok["price"] > 0 and ok["source"] == "nasdaq"
    assert "chart" in calls[1] and "fromDate=" in calls[1]
    missing = market.load(connection, 2)
    assert missing["error"] == "hittades inte hos Nasdaq" and missing["price"] is None
    assert market.load(connection, 3) is None
    assert sleeps                                              # paus mellan bolag
    again: list[str] = []
    market.run(connection, None, fetch=fake_fetch(again), sleep=lambda _: None, today=TODAY, log=lambda _: None)
    assert again == []                                         # redan hämtade idag
    tomorrow: list[str] = []
    market.run(connection, None, fetch=fake_fetch(tomorrow), sleep=lambda _: None, today="2026-11-08",
               log=lambda _: None)
    assert sum("chart" in u for u in tomorrow) == 1            # känt orderboks-id: bara ett anrop för bolag 1


def test_run_respects_limit(connection):
    counts = market.run(connection, None, limit=1, fetch=fake_fetch([]), sleep=lambda _: None, today=TODAY,
                        log=lambda _: None)
    assert counts["fetched"] + counts["failed"] == 1 and counts["remaining"] == 1


def test_weekly_series_keeps_the_last_close_per_week_for_one_year():
    import json
    from datetime import date, timedelta

    start = date(2025, 1, 6)                                    # en måndag
    points = [{"date": (start + timedelta(days=i)).isoformat(), "close": 10 + i, "high": None, "low": None, "volume": 100}
              for i in range(0, 500) if (start + timedelta(days=i)).weekday() < 5]
    series = market.weekly_series(points, date.fromisoformat(points[-1]["date"]))
    assert 52 <= len(series) <= 54 and series[-1] == [points[-1]["date"], points[-1]["close"]]
    assert all(date.fromisoformat(day).weekday() == 4 for day, _ in series[:-1])       # veckans sista handelsdag
    assert json.loads(market.summarize(points)["series"]) == series
