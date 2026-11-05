"""Runway från fakta. Regler kalibrerade mot facitsetet.

- Burn per kassaflödesperiod, med periodens egen längd.
- Längsta perioden ger huvudtalet (minst säsongsbrus). Bränner den senaste
  perioden över 1,5 gånger så fort visas den separat som varning, med egen
  runway, men ändrar inte huvudtalet.
- Investeringar: i första hand rörelseinvesteringar (investeringsflödet utan
  förvärv och finansiella placeringar), annars capex, och bara om båda saknas
  hela investeringskassaflödet (flaggas).
- Utflöden efter periodens slut som rapporten själv nämner (t.ex. amortering,
  fakta `post_period_outflow`) dras från kassan innan den rullas fram. Inflöden
  efter perioden (`post_period_inflow`) läggs inte till (de kommer från
  nyhetssteget) utan visas bara. Lån som förfaller inom 12 månader
  (`debt_due_12m`) ändrar inte huvudtalet utan ger en varning med egen runway.
- Kassan rullas fram med burn från periodslut till idag.
- Bekräftad finansiering efter rapporten (genomförda rundor, se rounds.py) läggs
  till kassan; annonserade rundor redovisas bara och flaggas.
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
    confirmed_financing: float = 0.0
    announced_financing: float = 0.0
    financing_events: list[dict] = field(default_factory=list)
    post_period_outflows: float = 0.0
    reported_inflows_after_period: float = 0.0
    debt_due_12m: float | None = None
    runway_after_debt: float | None = None

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


def _apply_financing(result: Runway, financing: tuple[float, float, list[dict]] | None) -> float:
    """Registrerar finansieringen och returnerar den bekräftade kassan som läggs till."""
    if financing is None:
        return 0.0
    confirmed, announced, events = financing
    result.confirmed_financing, result.announced_financing, result.financing_events = confirmed, announced, events
    if confirmed > 0:
        result.flags.append("financing_received")
    if any(e["status"] == "announced" and e["type"] != "repayment" for e in events):
        result.flags.append("raise_announced")
    return confirmed


def _sum(facts: list[Fact], kind: str) -> float:
    return sum(f.value for f in facts if f.kind == kind and f.value > 0)


def _debt_warning(result: Runway, facts: list[Fact], cash_today: float) -> None:
    """Lån som förfaller inom 12 mån: varning med runway efter amortering."""
    debt = _sum(facts, "debt_due_12m")
    if debt <= 0 or not result.monthly_burn:
        return
    result.debt_due_12m = debt
    result.runway_after_debt = round(max(0.0, cash_today - debt) / result.monthly_burn, 1)
    if result.runway_after_debt < (result.runway_today or 0) - 1:
        result.flags.append("debt_due_within_12m")


def compute(facts: list[Fact], period_end: str, *, today: str,
            financing: tuple[float, float, list[dict]] | None = None) -> Runway:
    result = Runway(status="ok", period_end=period_end)
    extra = _apply_financing(result, financing)
    cash = next((f.value for f in facts if f.kind == "cash" and f.period_end == period_end), None)
    if cash is None:
        result.status = "missing_cash"
        return result
    placements = next((f.value for f in facts if f.kind == "short_term_investments"
                       and f.period_end == period_end), 0.0)
    if placements:
        result.flags.append("includes_short_term_investments")
        cash += placements
    result.cash = cash
    # Samma amortering kan stå både i rapporten och i ett pressmeddelande: räkna den en gång.
    news_repayments = [e.get("amount") or 0.0 for e in result.financing_events
                       if e.get("type") == "repayment" and e.get("status") == "completed"]
    outflows = sum(f.value for f in facts if f.kind == "post_period_outflow" and f.value > 0
                   and not any(abs(f.value - r) <= 0.25 * max(f.value, r) for r in news_repayments))
    result.post_period_outflows = outflows
    result.reported_inflows_after_period = _sum(facts, "post_period_inflow")
    if outflows:
        result.flags.append("post_period_outflows")
        extra -= outflows
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
        result.recent_runway_today = round(
            max(0.0, cash + extra - recent.monthly_burn * since) / recent.monthly_burn, 1)
    if chosen.monthly_burn == 0:
        result.cash_flow_positive = True
        return result
    result.runway_at_report = round(cash / chosen.monthly_burn, 1)
    if (cash + extra) / chosen.monthly_burn < since:
        # Kassan borde redan vara slut: nästan alltid ny finansiering efter rapporten.
        result.flags.append("cash_exhausted_since_report")
    result.runway_today = round(max(0.0, cash + extra - chosen.monthly_burn * since) / chosen.monthly_burn, 1)
    _debt_warning(result, facts, max(0.0, cash + extra - chosen.monthly_burn * since))
    return result
