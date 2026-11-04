from __future__ import annotations

from capital_radar.runway_engine import Fact, FinancingEvent, Report, evaluate, period_months


def fact(kind, value, start=None, end="2026-06-30", as_of=None, confidence=0.9):
    return Fact(kind, value, "SEK", start, end, as_of, confidence, document_id=7)


def report(*facts, start="2026-01-01", end="2026-06-30"):
    return Report(issuer_id=1, document_id=7, period_start=start, period_end=end,
                  period_months=6, currency="SEK", facts=tuple(facts))


CASH = fact("cash_and_cash_equivalents", 12_000_000, as_of="2026-06-30")


def test_period_months_handles_quarter_half_year_and_full_year():
    assert period_months("2026-04-01", "2026-06-30") == 3
    assert period_months("2026-01-01", "2026-06-30") == 6
    assert period_months("2025-01-01", "2025-12-31") == 12
    assert period_months("2024-01-01", "2025-12-31") is None


def test_burn_uses_each_facts_own_period_and_picks_the_higher():
    # YTD 6 mån: -6 MSEK (1 MSEK/mån). Kvartal 3 mån: -4,5 MSEK (1,5 MSEK/mån).
    result = evaluate(report(CASH, fact("operating_cash_flow", -6_000_000, "2026-01-01"),
                             fact("operating_cash_flow", -4_500_000, "2026-04-01")), [], as_of="2026-06-30")
    assert result.status == "ok"
    assert result.monthly_burn == 1_500_000
    assert [c.months for c in result.burn_candidates] == [3, 6]
    assert result.runway_at_report_months == 8.0
    assert any("högsta burn" in text for text in result.assumptions)


def test_old_logic_bug_quarter_fact_in_half_year_report():
    # Rapporten är H1 men kassaflödet avser bara Q2: burn är 1 MSEK/mån, inte 0,5.
    result = evaluate(report(CASH, fact("operating_cash_flow", -3_000_000, "2026-04-01")), [], as_of="2026-06-30")
    assert result.monthly_burn == 1_000_000
    assert result.runway_at_report_months == 12.0


def test_capex_is_added_and_cash_rolls_forward():
    result = evaluate(report(CASH, fact("operating_cash_flow", -3_000_000, "2026-04-01"),
                             fact("capex", -1_500_000, "2026-04-01")), [], as_of="2026-09-30")
    assert result.monthly_burn == 1_500_000
    assert result.months_since_report == 3.0
    assert round(result.estimated_cash_today) == round(12_000_000 - 1_500_000 * 92 / 30.4375)
    assert result.safe_runway_months == 5.0


def test_safe_and_expected_runway_separate_confirmed_and_announced_financing():
    events = [
        FinancingEvent("t1", "completed", 3_000_000, "SEK", "2026-07-15"),
        FinancingEvent("t2", "announced", 6_000_000, "SEK", "2026-08-01"),
        FinancingEvent("t3", "completed", 9_000_000, "SEK", "2026-05-01"),  # före periodslut
        FinancingEvent("t4", "completed", 9_000_000, "EUR", "2026-07-01"),  # annan valuta
        FinancingEvent("t5", "completed", 9_000_000, "SEK", "2026-07-01", excluded_reason="Icke-kontant"),
    ]
    result = evaluate(report(CASH, fact("operating_cash_flow", -6_000_000, "2026-01-01")), events, as_of="2026-06-30")
    assert result.confirmed_financing == 3_000_000
    assert result.expected_financing == 6_000_000
    assert result.safe_runway_months == 15.0
    assert result.expected_runway_months == 21.0
    assert [e["transaction_id"] for e in result.included_events] == ["t1", "t2"]


def test_cash_flow_positive_has_no_runway_number():
    result = evaluate(report(CASH, fact("operating_cash_flow", 2_000_000, "2026-01-01")), [], as_of="2026-07-31")
    assert result.cash_flow_positive is True
    assert result.safe_runway_months is None
    assert result.monthly_burn == 0.0


def test_missing_data_and_stale_reports_are_explicit():
    assert evaluate(None, [], as_of="2026-07-01", issuer_id=5).status == "missing_report"
    assert evaluate(report(fact("operating_cash_flow", -1, "2026-01-01")), [], as_of="2026-07-01").status == "missing_cash"
    assert evaluate(report(CASH), [], as_of="2026-07-01").status == "missing_cash_flow"
    stale = evaluate(report(CASH, fact("operating_cash_flow", -6_000_000, "2026-01-01")), [], as_of="2027-08-01")
    assert "stale_report" in stale.flags
    assert "cash_likely_exhausted" in stale.flags
    assert stale.safe_runway_months == 0.0


def test_diverging_periods_are_flagged():
    result = evaluate(report(CASH, fact("operating_cash_flow", -600_000, "2026-01-01"),
                             fact("operating_cash_flow", -3_000_000, "2026-04-01")), [], as_of="2026-06-30")
    assert "burn_periods_diverge" in result.flags


def test_compare_runs_on_empty_database(tmp_path):
    import sqlite3

    from capital_radar.db import Database
    from capital_radar.runway_compare import compare, summarize

    db = Database(tmp_path / "screener.db")
    db.initialize()
    connection = sqlite3.connect(db.path)
    connection.row_factory = sqlite3.Row
    try:
        rows = compare(connection, as_of="2026-07-01")
    finally:
        connection.close()
    assert rows == []
    assert summarize(rows)["issuers"] == 0
