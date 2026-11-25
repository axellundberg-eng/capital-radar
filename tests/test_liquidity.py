"""Utförsäljning vid 20 % av dagsvolymen (liquidity.py), utan nätverk."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from radar import liquidity, market
from radar.db import connect


def days(n, start=date(2026, 1, 1), close=1.0, volume=100_000.0):
    return [{"date": (start + timedelta(days=i)).isoformat(), "close": close, "volume": volume} for i in range(n)]


def test_simulate_sells_at_most_twenty_percent_of_volume_after_registration():
    points = days(40, volume=100_000.0)
    sim = liquidity.simulate(points, "2026-01-01", 50_000.0, skip=10)
    # 20 % av 100 000 = 20 000 per dag: 20 000 + 20 000 + 10 000 = tre dagar
    assert sim["days"] == 3 and sim["complete"] is True
    assert sim["start"] == "2026-01-12"    # dag 1 efter utfallet är 2 jan; tio handelsdagar hoppas över
    assert sim["proceeds"] == pytest.approx(50_000.0)


def test_simulate_flags_stuck_when_volume_is_too_thin():
    points = days(100, volume=1_000.0)
    sim = liquidity.simulate(points, "2026-01-01", 1_000_000.0, skip=0, max_days=60)
    assert sim["complete"] is False and sim["open"] is False and sim["days"] == 60
    assert sim["sold"] == pytest.approx(60 * 200.0)


def test_simulate_without_data_after_the_outcome_is_none():
    assert liquidity.simulate(days(5), "2026-01-03", 100.0) is None


def test_capacity_uses_twenty_percent_of_turnover():
    c = liquidity.capacity(500_000.0, 1_000_000.0, "median_60d")
    assert c["sell_per_day"] == 100_000 and c["days_full_utilization"] == 10.0
    assert c["max_guarantee_10d"] == 1_000_000 and c["max_guarantee_20d"] == 2_000_000
    assert liquidity.capacity(None)["days_full_utilization"] is None


def test_median_turnover_ignores_spikes():
    points = days(60, volume=10_000.0)
    points[30]["volume"] = 10_000_000.0
    assert liquidity.median_turnover(points) == pytest.approx(10_000.0)


def row(**fields):
    base = {"status": "announced", "currency": "SEK", "gross_amount": None, "guarantee_amount": None,
            "guarantee_fee_pct": None, "guarantee_utilized": None, "subscription_rate_pct": None,
            "issue_price": None, "guarantors": None}
    return {**base, **fields}


def test_unused_guarantee_paid_in_shares_is_sold_with_the_volume_rule():
    rows = [row(type="warrants", event_date="2026-01-01", guarantee_amount=3_700_000.0, guarantee_fee_pct=15.0,
                issue_price=0.025, guarantors="A; B"),
            row(type="set_off", event_date="2026-01-01", guarantee_fee_pct=17.0, issue_price=0.025),
            row(type="warrants", event_date="2026-01-20", status="completed", subscription_rate_pct=81.5,
                guarantee_utilized=0.0)]
    points = days(80, close=0.03, volume=10_000_000.0)
    deal = liquidity.block(points, rows, None)["deals"][0]
    assert deal["fee_in_shares"] is True and deal["fee_pct"] == 17.0 and deal["utilized_share"] == 0.0
    # 170 000 kr i aktier à 0,025 = 6,8 M aktier, sålda till 0,03 = 204 000 kr; 2 M aktier per dag = 4 dagar
    assert deal["result"] == 204_000 and deal["result_pct"] == 20.4
    assert deal["exit"]["days"] == 4 and deal["exit"]["complete"] is True


def test_cash_fee_with_full_subscription_needs_no_selling():
    rows = [row(type="rights_issue", event_date="2026-02-01", guarantee_amount=8_000_000.0, guarantee_fee_pct=12.0,
                issue_price=1.3),
            row(type="rights_issue", event_date="2026-02-20", status="completed", subscription_rate_pct=100.0)]
    deal = liquidity.block([], rows, None)["deals"][0]
    assert deal["result"] == 120_000 and deal["exit"] is None


def test_estimated_utilization_from_subscription_rate():
    rows = [row(type="rights_issue", event_date="2026-02-01", gross_amount=20_000_000.0,
                guarantee_amount=10_000_000.0, guarantee_fee_pct=10.0, issue_price=1.0),
            row(type="rights_issue", event_date="2026-02-20", status="completed", subscription_rate_pct=60.0)]
    terms = liquidity.deal_terms(liquidity._groups(rows)[0], rows)
    # 40 % av 20 MSEK = 8 MSEK saknas, garantin 10 MSEK tas i anspråk till 80 %
    assert terms["utilized_share"] == pytest.approx(0.8) and terms["utilized_estimated"] is True
    deal = liquidity.deal_return(terms, days(100, start=date(2026, 2, 1), close=0.5, volume=10_000_000.0))
    # 800 000 aktier à 1 kr säljs till 0,50: 400 000 in, 800 000 betalt, 100 000 i ersättning
    assert deal["result"] == -300_000


def test_missing_outcome_gives_reason_not_a_number():
    rows = [row(type="rights_issue", event_date="2026-02-01", guarantee_amount=1e6, guarantee_fee_pct=10.0)]
    deal = liquidity.block([], rows, None)["deals"][0]
    assert deal["result"] is None and deal["missing"] == "utfallet saknas"


def test_daily_points_are_stored_and_loaded(tmp_path):
    connection = connect(tmp_path / "radar.db")
    market.store_daily(connection, 7, [{"date": "2026-01-02", "close": 1.5, "volume": 100.0}])
    market.store_daily(connection, 7, [{"date": "2026-01-02", "close": 1.6, "volume": 120.0},
                                       {"date": "2026-01-01", "close": 1.4, "volume": None}])
    assert market.load_daily(connection, 7) == [{"date": "2026-01-01", "close": 1.4, "volume": None},
                                               {"date": "2026-01-02", "close": 1.6, "volume": 120.0}]


def test_stated_utilization_wins_over_estimate_when_outcome_rows_share_a_date():
    rows = [row(type="warrants", event_date="2026-01-01", gross_amount=5_300_000.0, guarantee_amount=3_700_000.0,
                guarantee_fee_pct=15.0, issue_price=0.025),
            row(type="warrants", event_date="2026-01-20", status="completed", subscription_rate_pct=81.55),
            row(type="warrants", event_date="2026-01-20", status="completed", subscription_rate_pct=81.55,
                guarantee_utilized=0.0)]
    terms = liquidity.deal_terms(liquidity._groups(rows)[0], rows)
    assert terms["utilized_share"] == 0.0 and terms["utilized_estimated"] is False


def test_simulate_refuses_when_price_data_starts_after_the_outcome():
    points = days(100, start=date(2026, 3, 1))
    assert liquidity.simulate(points, "2026-01-15", 1_000.0) is None


def test_later_warrant_exercise_is_not_the_rights_issue_outcome():
    rows = [row(type="rights_issue", event_date="2025-11-04", gross_amount=18_500_000.0, guarantee_amount=18_500_000.0,
                guarantee_fee_pct=12.0, issue_price=0.078),
            row(type="rights_issue", event_date="2025-12-01", status="completed", subscription_rate_pct=100.0),
            row(type="warrants", event_date="2025-12-17", status="completed", subscription_rate_pct=0.01)]
    terms = liquidity.deal_terms(liquidity._groups(rows)[0], rows)
    assert terms["subscription_rate_pct"] == 100.0 and terms["utilized_share"] == 0.0


def test_posts_chained_within_the_gap_are_one_deal():
    rows = [row(type="rights_issue", event_date="2025-04-16", guarantee_amount=15e6),
            row(type="rights_issue", event_date="2025-05-27", guarantee_amount=15e6),
            row(type="rights_issue", event_date="2025-06-13", guarantee_fee_pct=12.0)]
    assert len(liquidity._groups(rows)) == 1
