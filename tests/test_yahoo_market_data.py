from __future__ import annotations

from pathlib import Path

from capital_radar.db import Database
from capital_radar.yahoo_market_data import (
    YahooFinanceMarketDataProvider,
    five_trading_day_metrics,
    parse_daily_bars,
    persist_post_outcome_metrics,
    yahoo_symbol,
)


def _chart_payload() -> dict:
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [
                        1_704_758_400, 1_704_844_800, 1_704_931_200,
                        1_705_017_600, 1_705_104_000,
                    ],
                    "indicators": {"quote": [{"close": [10, 12, 11, 13, 14], "volume": [100, 200, 0, 100, 100]}]},
                }
            ]
        }
    }


def test_swedish_symbol_and_metadata_override() -> None:
    assert yahoo_symbol("ACRI A", "Nasdaq First North Growth Market Sweden") == "ACRI-A.ST"
    assert yahoo_symbol("ignored", "First North", '{"yahoo_symbol":"acri-a.st"}') == "ACRI-A.ST"


def test_five_sessions_exclude_outcome_and_calculate_close_and_volume_weighted_average() -> None:
    bars = [
        {"date": "2024-01-01", "close": 9.0, "volume": 100},
        {"date": "2024-01-02", "close": 10.0, "volume": 100},
        {"date": "2024-01-03", "close": 12.0, "volume": 200},
        {"date": "2024-01-04", "close": 11.0, "volume": 0},
        {"date": "2024-01-05", "close": 13.0, "volume": 100},
        {"date": "2024-01-08", "close": 14.0, "volume": 100},
    ]
    metrics = five_trading_day_metrics("2024-01-01", bars)
    assert metrics["trading_dates"] == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    assert metrics["average_close"] == 12.0
    assert metrics["volume_weighted_average_close"] == 12.2
    assert metrics["total_volume"] == 500.0
    assert metrics["daily_bars"][0] == {"date": "2024-01-02", "close": 10.0, "volume": 100}


def test_incomplete_window_stays_null() -> None:
    metrics = five_trading_day_metrics("2024-01-01", [{"date": "2024-01-02", "close": 10, "volume": 100}])
    assert not metrics["complete"]
    assert metrics["average_close"] is None


def test_provider_resolves_security_and_labels_yahoo_source(tmp_path: Path) -> None:
    db = Database(tmp_path / "market.db")
    db.initialize()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO sources (
                id, name, source_kind, base_url, evidence_tier, authority,
                access_status, terms_status, created_at, updated_at
            ) VALUES ('test', 'Test', 'test', 'https://example.test', 1,
                      'test', 'allowed', 'unknown', '2024-01-01', '2024-01-01')"""
        )
        issuer_id = int(connection.execute(
            """INSERT INTO issuers (canonical_name, normalized_name, created_at, updated_at)
               VALUES ('Example AB', 'example ab', '2024-01-01', '2024-01-01')"""
        ).lastrowid)
        connection.execute(
        """INSERT INTO securities (issuer_id, source_id, external_id, isin, symbol, display_name,
               currency, market, segment, active, first_seen_at, last_seen_at, metadata_json)
               VALUES (?, 'test', 'example', 'SE0000000001', 'EXAMPLE', 'Example', 'SEK', 'Nasdaq Stockholm',
               'Small Cap', 1, '2024-01-01', '2024-01-01', '{}')""",
            (issuer_id,),
        )
    provider = YahooFinanceMarketDataProvider(db, transport=lambda _url: _chart_payload())
    metrics = provider.post_outcome_metrics(issuer_id, "2024-01-01")
    assert metrics is not None
    assert metrics["symbol"] == "EXAMPLE.ST"
    assert metrics["source"] == "yahoo_finance_chart"
    assert metrics["complete"]

    with db.connect() as connection:
        event_id = int(connection.execute(
            """INSERT INTO financing_events (
                issuer_id, event_fingerprint, event_type, description, status,
                cash_direction, liquidity_effect_kind, reflected_in_report,
                confidence, created_at, updated_at
            ) VALUES (?, 'event-1', 'rights_issue', 'Outcome', 'completed', 'inflow',
                      'confirmed_cash', 'no', 0.9, '2024-01-01', '2024-01-01')""",
            (issuer_id,),
        ).lastrowid)
    persist_post_outcome_metrics(db, event_id, metrics)
    with db.connect() as connection:
        stored = connection.execute(
            """SELECT close_average_5d, volume_weighted_close_5d, total_volume_5d,
                      source, complete FROM financing_outcome_market_metrics
               WHERE financing_event_id=?""",
            (event_id,),
        ).fetchone()
    assert stored["source"] == "yahoo_finance_chart"
    assert stored["complete"] == 1
    with db.connect() as connection:
        daily_rows = connection.execute(
            """SELECT trading_date, close, volume FROM financing_outcome_market_days
               WHERE financing_event_id=? ORDER BY trading_date""",
            (event_id,),
        ).fetchall()
    assert len(daily_rows) == 5
    assert daily_rows[0]["volume"] == 100.0


def test_parse_daily_bars_discards_missing_closes() -> None:
    payload = _chart_payload()
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    assert len(parse_daily_bars(payload)) == 4
