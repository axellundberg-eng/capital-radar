"""Yahoo Finance daily-price adapter for source-labelled financing analysis.

The adapter uses Yahoo's chart response for daily close and volume data.  It
does not make valuation or liquidity assumptions: a five-trading-day metric is
available only when all five sessions after an outcome date are present.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_CHART_FALLBACK_BASE_URL = "https://query2.finance.yahoo.com/v8/finance/chart"
YAHOO_SOURCE = "yahoo_finance_chart"


def yahoo_symbol(symbol: str, market: str | None, metadata_json: str | None = None) -> str:
    """Resolve a Yahoo symbol using saved security metadata before a Swedish fallback.

    ``metadata_json.yahoo_symbol`` is authoritative.  The fallback is limited
    to Swedish venues because ``.ST`` is the Yahoo suffix for those listings;
    other markets must provide an explicit Yahoo symbol.
    """
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            metadata = {}
        configured = metadata.get("yahoo_symbol") if isinstance(metadata, dict) else None
        if isinstance(configured, str) and configured.strip():
            return configured.strip().upper()

    normalized = "-".join(str(symbol or "").upper().split())
    if not normalized:
        raise ValueError("A security symbol is required for Yahoo market data")
    if "." in normalized:
        return normalized
    market_name = (market or "").casefold()
    if any(token in market_name for token in ("sweden", "stockholm", "first north")):
        return f"{normalized}.ST"
    raise ValueError("Yahoo symbol must be configured for a non-Swedish venue")


def _epoch(value: date) -> int:
    return int(datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def chart_url(symbol: str, start: date, end_exclusive: date) -> str:
    params = urlencode(
        {
            "period1": _epoch(start),
            "period2": _epoch(end_exclusive),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    return f"{YAHOO_CHART_BASE_URL}/{quote(symbol, safe='.-')}?{params}"


def parse_daily_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse valid daily close/volume observations from a chart response."""
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp") or []
    quote_rows = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_rows.get("close") or [] if isinstance(quote_rows, dict) else []
    volumes = quote_rows.get("volume") or [] if isinstance(quote_rows, dict) else []
    timezone_name = ((result.get("meta") or {}).get("exchangeTimezoneName"))
    try:
        exchange_timezone = ZoneInfo(str(timezone_name)) if timezone_name else timezone.utc
    except (TypeError, ValueError):
        exchange_timezone = timezone.utc
    bars: list[dict[str, Any]] = []
    for timestamp, close, volume in zip(timestamps, closes, volumes):
        if close is None:
            continue
        try:
            session_date = datetime.fromtimestamp(int(timestamp), tz=exchange_timezone).date().isoformat()
            close_value = float(close)
            volume_value = float(volume) if volume is not None else None
        except (TypeError, ValueError, OverflowError):
            continue
        bars.append({"date": session_date, "close": close_value, "volume": volume_value})
    return bars


def five_trading_day_metrics(outcome_date: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate post-outcome simple average and volume-weighted close.

    The outcome session is excluded.  Holidays and weekends are naturally
    skipped because the calculation uses returned trading sessions only.
    """
    try:
        outcome = date.fromisoformat(outcome_date)
    except (TypeError, ValueError):
        raise ValueError("outcome_date must be YYYY-MM-DD") from None
    selected = [bar for bar in bars if date.fromisoformat(str(bar["date"])) > outcome][:5]
    complete = len(selected) == 5
    if not complete:
        return {
            "outcome_date": outcome_date,
            "trading_dates": [bar["date"] for bar in selected],
            "session_count": len(selected),
            "complete": False,
            "average_close": None,
            "volume_weighted_average_close": None,
            "total_volume": None,
            "daily_bars": selected,
        }
    average_close = sum(float(bar["close"]) for bar in selected) / len(selected)
    volume_bars = [bar for bar in selected if bar.get("volume") is not None and float(bar["volume"]) > 0]
    total_volume = sum(float(bar["volume"]) for bar in volume_bars)
    return {
        "outcome_date": outcome_date,
        "trading_dates": [bar["date"] for bar in selected],
        "session_count": len(selected),
        "complete": True,
        "average_close": average_close,
        "volume_weighted_average_close": (
            sum(float(bar["close"]) * float(bar["volume"]) for bar in volume_bars) / total_volume
            if total_volume else None
        ),
        "total_volume": total_volume or None,
        "daily_bars": selected,
    }


class YahooFinanceMarketDataProvider:
    """Fetch daily Yahoo chart data for a security stored in the Screener DB."""

    def __init__(
        self,
        db: Any,
        *,
        transport: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self._transport = transport or self._request_json

    @staticmethod
    def _request_json(url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "LundbergsCapitalScreener/1.0"})
        with urlopen(request, timeout=20) as response:  # nosec B310 - fixed Yahoo host
            return json.loads(response.read().decode("utf-8"))

    def post_outcome_metrics(self, issuer_id: int, outcome_date: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            security = connection.execute(
                """SELECT symbol, market, metadata_json FROM securities
                   WHERE issuer_id=? AND active=1 ORDER BY id LIMIT 1""",
                (issuer_id,),
            ).fetchone()
        if security is None:
            return None
        try:
            symbol = yahoo_symbol(security["symbol"], security["market"], security["metadata_json"])
            outcome = date.fromisoformat(outcome_date)
        except (TypeError, ValueError):
            return None
        url = chart_url(symbol, outcome, outcome + timedelta(days=45))
        try:
            bars = parse_daily_bars(self._transport(url))
        except (OSError, ValueError, json.JSONDecodeError):
            # Query2 is a bounded fallback for transient/host failures only.
            fallback_url = url.replace(YAHOO_CHART_BASE_URL, YAHOO_CHART_FALLBACK_BASE_URL, 1)
            try:
                bars = parse_daily_bars(self._transport(fallback_url))
                url = fallback_url
            except (OSError, ValueError, json.JSONDecodeError):
                return None
        metrics = five_trading_day_metrics(outcome_date, bars)
        metrics.update({"issuer_id": issuer_id, "symbol": symbol, "source": YAHOO_SOURCE, "source_url": url})
        return metrics


def persist_post_outcome_metrics(db: Any, financing_event_id: int, metrics: dict[str, Any]) -> None:
    """Store a complete or incomplete outcome window with its exact provenance."""
    required = ("outcome_date", "symbol", "source", "source_url", "trading_dates", "complete", "daily_bars")
    if any(key not in metrics for key in required):
        raise ValueError("post-outcome metrics are missing required provenance")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    with db.connect() as connection:
        connection.execute(
            """
            INSERT INTO financing_outcome_market_metrics (
                financing_event_id, outcome_date, symbol, close_average_5d,
                volume_weighted_close_5d, total_volume_5d, trading_dates_json,
                source, source_url, retrieved_at, complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(financing_event_id) DO UPDATE SET
                outcome_date=excluded.outcome_date,
                symbol=excluded.symbol,
                close_average_5d=excluded.close_average_5d,
                volume_weighted_close_5d=excluded.volume_weighted_close_5d,
                total_volume_5d=excluded.total_volume_5d,
                trading_dates_json=excluded.trading_dates_json,
                source=excluded.source,
                source_url=excluded.source_url,
                retrieved_at=excluded.retrieved_at,
                complete=excluded.complete
            """,
            (
                financing_event_id,
                metrics["outcome_date"],
                metrics["symbol"],
                metrics.get("average_close"),
                metrics.get("volume_weighted_average_close"),
                metrics.get("total_volume"),
                json.dumps(metrics["trading_dates"]),
                metrics["source"],
                metrics["source_url"],
                retrieved_at,
                int(bool(metrics["complete"])),
            ),
        )
        connection.execute(
            "DELETE FROM financing_outcome_market_days WHERE financing_event_id=?",
            (financing_event_id,),
        )
        for bar in metrics["daily_bars"]:
            connection.execute(
                """
                INSERT INTO financing_outcome_market_days (
                    financing_event_id, trading_date, close, volume, source, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    financing_event_id,
                    bar["date"],
                    bar["close"],
                    bar.get("volume"),
                    metrics["source"],
                    retrieved_at,
                ),
            )


__all__ = [
    "YAHOO_CHART_BASE_URL",
    "YAHOO_CHART_FALLBACK_BASE_URL",
    "YAHOO_SOURCE",
    "YahooFinanceMarketDataProvider",
    "chart_url",
    "five_trading_day_metrics",
    "parse_daily_bars",
    "persist_post_outcome_metrics",
    "yahoo_symbol",
]
