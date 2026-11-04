"""Runway från fakta. Regler kalibrerade mot facitsetet.

- Burn per kassaflödesperiod, med periodens egen längd.
- Längsta perioden ger huvudtalet (minst säsongsbrus). Bränner den senaste
  perioden över 1,5 gånger så fort visas den separat som varning, med egen
  runway, men ändrar inte huvudtalet.
- Investeringar: i första hand rörelseinvesteringar (investeringsflödet utan
  förvärv och finansiella placeringar), annars capex, och bara om båda saknas
  hela investeringskassaflödet (flaggas).
- Kassan rullas fram med burn från periodslut till idag.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

DAYS_PER_MONTH = 30.4375
RECENT_RATIO = 1.5
STALE_MONTHS = 7.0


@dataclass(frozen=True)
class Fact:
    kind: str
    value: float
    period_start: str | None
    period_end: str | None


@dataclass
class Period:
    start: str
    months: int
    operating_cash_flow: float
    investments: float
    investments_basis: str
    monthly_burn: float


@dataclass
class Runway:
    status: str
    period_end: str | None = None
    cash: float | None = None
    monthly_burn: float | None = None
    cash_flow_positive: bool = False
    months_since_report: float | None = None
    runway_at_report: float | None = None
    runway_today: float | None = None
    recent_monthly_burn: float | None = None
    recent_runway_today: float | None = None
    periods: list[Period] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _months(start: str, end: str) -> float:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days / DAYS_PER_MONTH


def period_length(start: str, end: str) -> int | None:
    months = round(_months(start, end) + 1 / DAYS_PER_MONTH)
    return months if 1 <= months <= 12 else None


def _periods(facts: list[Fact], period_end: str) -> list[Period]:
    def at(kind: str, start: str) -> Fact | None:
        return next((f for f in facts if f.kind == kind and f.period_start == start
                     and f.period_end == period_end), None)

    periods = []
    starts = sorted({f.period_start for f in facts if f.kind == "operating_cash_flow"
                     and f.period_end == period_end and f.period_start})
    for start in starts:
        months = period_length(start, period_end)
        if months is None:
            continue
        cfo = at("operating_cash_flow", start).value
        operating = at("operating_investments", start)
        capex_fact, investing_fact = at("capex", start), at("investing_cash_flow", start)
        if operating is not None:
            # Netto: ett positivt tal (avyttringar större än investeringar) minskar burnen.
            amount, basis = -operating.value, "operating_investments"
        elif capex_fact is not None:
            amount, basis = abs(capex_fact.value), "capex"
        elif investing_fact is not None and investing_fact.value < 0:
            amount, basis = -investing_fact.value, "investing_cash_flow"
        else:
            amount, basis = 0.0, "none"
        burn = max(0.0, (amount - cfo) / months)
        periods.append(Period(start, months, cfo, amount, basis, burn))
    return sorted(periods, key=lambda p: p.months)


def compute(facts: list[Fact], period_end: str, *, today: str) -> Runway:
    result = Runway(status="ok", period_end=period_end)
    cash = next((f.value for f in facts if f.kind == "cash" and f.period_end == period_end), None)
    if cash is None:
        result.status = "missing_cash"
        return result
    result.cash = cash
    result.periods = _periods(facts, period_end)
    if not result.periods:
        result.status = "missing_cash_flow"
        return result
    chosen, recent = result.periods[-1], result.periods[0]
    faster_recent = (len(result.periods) > 1 and recent.monthly_burn > 0
                     and recent.monthly_burn > chosen.monthly_burn * RECENT_RATIO)
    if chosen.investments_basis == "investing_cash_flow":
        result.flags.append("investing_as_capex")
    since = max(0.0, _months(period_end, today))
    result.months_since_report = round(since, 1)
    if since > STALE_MONTHS:
        result.flags.append("stale_report")
    result.monthly_burn = chosen.monthly_burn
    if faster_recent:
        result.flags.append("recent_period_burn_higher")
        result.recent_monthly_burn = recent.monthly_burn
        result.recent_runway_today = round(max(0.0, cash - recent.monthly_burn * since) / recent.monthly_burn, 1)
    if chosen.monthly_burn == 0:
        result.cash_flow_positive = True
        return result
    result.runway_at_report = round(cash / chosen.monthly_burn, 1)
    result.runway_today = round(max(0.0, cash - chosen.monthly_burn * since) / chosen.monthly_burn, 1)
    return result
