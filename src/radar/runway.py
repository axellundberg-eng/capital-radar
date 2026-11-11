"""Runway från fakta. Regler kalibrerade mot facitsetet.

- Burn per kassaflödesperiod, med periodens egen längd: investeringar plus amortering av
  leasingskuld (IFRS 16, ligger i finansieringsflödet) minus löpande kassaflöde.
- Rullande tolv månader (LTM) ger huvudtalet när jämförelsekolumnerna finns: föregående helår plus
  innevarande period minus samma period föregående år. Annars ger den längsta perioden huvudtalet
  (minst säsongsbrus). Är bolaget positivt men senaste perioden bränner flaggas `recent_period_burning`. Bränner den senaste
  perioden över 1,5 gånger så fort visas den separat som varning, med egen
  runway, men ändrar inte huvudtalet.
- Investeringar: i första hand rörelseinvesteringar (investeringsflödet utan
  förvärv och finansiella placeringar), annars capex, och bara om båda saknas
  hela investeringskassaflödet (flaggas).
- Utflöden efter periodens slut som rapporten själv nämner (t.ex. amortering,
  fakta `post_period_outflow`) dras från kassan innan den rullas fram. Inflöden
  efter perioden som rapporten nämner (`post_period_inflow`, t.ex. försäljningslikvid,
  förlikning, inbetald fordran) läggs till, utom när en finansieringshändelse från
  nyhetssteget med liknande belopp redan räknas (då är det samma pengar). Lån som förfaller inom 12 månader
  (`debt_due_12m`) ändrar inte huvudtalet utan ger en varning med egen runway.
- Kassan rullas fram med burn från periodslut till idag.
- Kreditfaciliteter är en reserv: outnyttjad kredit (rapport + nyheter) ger `runway_with_credit`
  men ändrar inte huvudtalet. Runway under en månad utan ny finansiering flaggas `acute`.
- Strukturella händelser efter perioden (omvänt förvärv, fusion m.m.) ändrar inte talen men
  flaggas (`structural_change`, `public_offer`, `delisting`): rapportens siffror kan vara inaktuella.
- Bekräftad finansiering efter rapporten (genomförda rundor, se rounds.py) läggs
  till kassan; annonserade rundor redovisas bara och flaggas.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .rounds import EQUITY_COST, NON_CASH_TYPES, OUTFLOW_TYPES

DAYS_PER_MONTH = 30.4375
RECENT_RATIO = 1.5
# Rullande tolv månader med en stor engångsrörelse (t.ex. ett förskott som förbrukats förra året) kan ge
# många gånger högre burn än både innevarande år och senaste kvartalet (Gold Town Games: 814 000 mot
# 109 000 kr/mån). Huvudtalet bygger fortfarande på snittet (försiktigt), men runway i senaste takt visas
# bredvid, räknad på det högsta av årets snitt och senaste kvartalet.
LTM_DISTORTED_RATIO = 3.0
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
    lease: float = 0.0          # amortering av leasingskuld under perioden


@dataclass
class Runway:
    status: str
    period_end: str | None = None
    cash: float | None = None
    monthly_burn: float | None = None
    cash_flow_positive: bool = False
    months_since_report: float | None = None
    runway_at_report: float | None = None
    runway_today: float | None = None          # kassa + outnyttjad kredit, ÷ burn
    runway_cash_today: float | None = None     # bara kassan (ingen kredit), ÷ burn
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
    available_credit: float = 0.0
    runway_with_credit: float | None = None
    structural_events: list[dict] = field(default_factory=list)   # se events.py
    ltm: dict | None = None             # rullande tolv månader, om jämförelsekolumnerna finns
    burn_basis: str | None = None       # ltm, annual eller longest_period
    cash_today: float | None = None     # kassa idag (rapportens kassa rullad fram), rapportens valuta
    currency: str | None = None         # rapportens valuta
    # Uppdelning av finansieringen efter rapporten (rapportens valuta, netto kassaeffekt); summan av
    # equity_after_report och debt_after_report är confirmed_financing.
    equity_after_report: float = 0.0    # emissioner, teckningsoptioner och övrigt
    debt_after_report: float = 0.0      # lån, brygglån, konvertibler, utnyttjad kredit minus återbetalningar
    equity_announced: float = 0.0       # annonserat men inte genomfört
    debt_announced: float = 0.0
    secured_financing: float = 0.0      # säkrad del (åtaganden + garantier) av annonserade emissioner, netto
    runway_pro_forma: float | None = None   # runway om den säkrade delen kommer in

    def to_dict(self) -> dict:
        return asdict(self)


def _months(start: str, end: str) -> float:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days / DAYS_PER_MONTH


def period_length(start: str, end: str) -> int | None:
    months = round(_months(start, end) + 1 / DAYS_PER_MONTH)
    return months if 1 <= months <= 12 else None


def _flows(facts: list[Fact], start: str, end: str) -> tuple[float, float, str, float] | None:
    """(löpande kassaflöde, investeringar, investeringsunderlag, leasingamortering) för en period."""
    def at(kind: str) -> Fact | None:
        return next((f for f in facts if f.kind == kind and f.period_start == start and f.period_end == end), None)

    cfo = at("operating_cash_flow")
    if cfo is None:
        return None
    operating, capex_fact, investing_fact = at("operating_investments"), at("capex"), at("investing_cash_flow")
    if operating is not None:
        # Netto: ett positivt tal (avyttringar större än investeringar) minskar burnen.
        amount, basis = -operating.value, "operating_investments"
    elif capex_fact is not None:
        amount, basis = abs(capex_fact.value), "capex"
    elif investing_fact is not None and investing_fact.value < 0:
        amount, basis = -investing_fact.value, "investing_cash_flow"
    else:
        amount, basis = 0.0, "none"
    lease = at("lease_repayment")
    return cfo.value, amount, basis, abs(lease.value) if lease is not None else 0.0


def _periods(facts: list[Fact], period_end: str) -> list[Period]:
    periods = []
    starts = sorted({f.period_start for f in facts if f.kind == "operating_cash_flow"
                     and f.period_end == period_end and f.period_start})
    for start in starts:
        months = period_length(start, period_end)
        flows = _flows(facts, start, period_end)
        if months is None or flows is None:
            continue
        cfo, amount, basis, lease = flows
        burn = max(0.0, (amount + lease - cfo) / months)
        periods.append(Period(start, months, cfo, amount, basis, burn, lease))
    return sorted(periods, key=lambda p: p.months)


def _year_earlier(day: str) -> str:
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:  # 29 februari
        return d.replace(year=d.year - 1, day=28).isoformat()


def _ltm(facts: list[Fact], periods: list[Period], period_end: str) -> dict | None:
    """Rullande tolv månader: föregående helår + innevarande period - samma period föregående år."""
    if not periods:
        return None
    ytd = periods[-1]
    if ytd.months == 12:
        return {"months": 12, "operating_cash_flow": ytd.operating_cash_flow, "investments": ytd.investments,
                "lease": ytd.lease, "monthly_burn": ytd.monthly_burn, "basis": "annual"}
    prior = _flows(facts, _year_earlier(ytd.start), _year_earlier(period_end))
    year_end = (date.fromisoformat(ytd.start) - timedelta(days=1)).isoformat()
    year_starts = sorted({f.period_start for f in facts if f.kind == "operating_cash_flow"
                          and f.period_end == year_end and f.period_start
                          and period_length(f.period_start, year_end) == 12})
    year = _flows(facts, year_starts[0], year_end) if year_starts else None
    if prior is None or year is None:
        return None
    cfo = year[0] + ytd.operating_cash_flow - prior[0]
    investments = year[1] + ytd.investments - prior[1]
    lease = year[3] + ytd.lease - prior[3]
    return {"months": 12, "operating_cash_flow": cfo, "investments": investments, "lease": lease,
            "monthly_burn": max(0.0, (investments + lease - cfo) / 12), "basis": "ltm"}


DEBT_SPLIT_TYPES = ("loan", "bridge_loan", "convertible", "credit_facility", "repayment")


def _apply_financing(result: Runway, financing: tuple | None) -> float:
    """Registrerar finansieringen och returnerar den bekräftade kassan som läggs till."""
    if financing is None:
        return 0.0
    confirmed, announced, events = financing[:3]
    result.confirmed_financing, result.announced_financing, result.financing_events = confirmed, announced, events
    result.available_credit = financing[3] if len(financing) > 3 else 0.0
    if confirmed > 0:
        result.flags.append("financing_received")
    # Allt annonserat som tillför pengar (emission, lån, brygglån, kreditfacilitet) väntar på utfall.
    if any(e["status"] == "announced" and e["type"] not in OUTFLOW_TYPES + NON_CASH_TYPES for e in events):
        result.flags.append("raise_announced")
    if any(e.get("unconverted") for e in events):
        result.flags.append("unconverted_currency")   # kurs saknades: beloppet räknas inte
    for e in events:
        if e.get("unconverted") or e.get("type") in NON_CASH_TYPES:
            continue
        debt = e.get("type") in DEBT_SPLIT_TYPES
        if e["status"] == "completed":
            if debt:
                result.debt_after_report += e.get("cash") or 0.0
            else:
                result.equity_after_report += e.get("cash") or 0.0
        elif e["status"] == "announced" and e.get("amount"):
            if debt:
                result.debt_announced += e["amount"]
            else:
                result.equity_announced += e["amount"]
    return confirmed


def _apply_structural(result: Runway, structural: tuple | None) -> None:
    """Strukturella händelser efter perioden: bara redovisning och flaggor, ingen ändring av talen."""
    if structural:
        result.structural_events = list(structural[0])
        result.flags.extend(structural[1])


def _sum(facts: list[Fact], kind: str) -> float:
    return sum(f.value for f in facts if f.kind == kind and f.value > 0)


def _debt_warning(result: Runway, facts: list[Fact], cash_today: float) -> None:
    """Lån som förfaller inom 12 mån: varning med runway efter amortering.

    Skulden gäller vid periodslut; det som redan amorterats efter perioden (rapportens
    händelser och pressmeddelanden) dras av, annars räknas samma betalning två gånger.
    """
    repaid = result.post_period_outflows + sum(
        e.get("amount") or 0.0 for e in result.financing_events
        if e.get("type") == "repayment" and e.get("status") == "completed")
    debt = max(0.0, _sum(facts, "debt_due_12m") - repaid)
    if debt <= 0 or not result.monthly_burn:
        return
    result.debt_due_12m = debt
    result.runway_after_debt = round(max(0.0, cash_today - debt) / result.monthly_burn, 1)
    if result.runway_after_debt < (result.runway_today or 0) - 1:
        result.flags.append("debt_due_within_12m")


def _pro_forma(result: Runway, cash_today: float, burn: float) -> None:
    """Annonserade emissioner räknas inte i huvudtalet. Den del som redan är säkrad genom
    teckningsåtaganden och garantier visas som pro forma-runway: nettot efter angivna kostnader,
    annars schablonen."""
    secured = 0.0
    for e in result.financing_events:
        if e.get("status") != "announced" or e.get("type") not in ("rights_issue", "directed_issue") \
                or e.get("unconverted"):
            continue
        gross = e.get("gross_amount")
        covered = (e.get("subscription_commitments") or 0.0) + (e.get("guarantee_amount") or 0.0)
        if not gross or covered <= 0:
            continue
        covered = min(gross, covered)
        cost_share = (e["issue_costs"] / gross) if e.get("issue_costs") else 1 - EQUITY_COST
        secured += covered * (1 - cost_share)
    if secured > 0:
        result.secured_financing = secured
        result.runway_pro_forma = round((cash_today + result.available_credit + secured) / burn, 1)
        result.flags.append("secured_pro_forma")


def _credit_and_acute(result: Runway, cash_today: float) -> None:
    """Kreditreserv ger egen runway (ändrar inte huvudtalet); akut = under en månad utan ny finansiering."""
    if result.available_credit > 0:
        result.runway_with_credit = round((cash_today + result.available_credit) / result.monthly_burn, 1)
        result.flags.append("credit_reserve")
    # Ny finansiering in efter rapporten (emission, lån, brygglån, dragen kredit) häver "akut".
    refinanced = any(e["status"] == "completed" and (e.get("cash") or 0) > 0 for e in result.financing_events)
    # Akut mäts mot kassa plus outnyttjad kredit: en kreditlinje som räcker i flera månader gör inte bolaget akut.
    if (result.runway_today or 0) < 1 and not refinanced:
        result.flags.append("acute")


def compute(facts: list[Fact], period_end: str, *, today: str,
            financing: tuple | None = None, structural: tuple | None = None) -> Runway:
    result = Runway(status="ok", period_end=period_end)
    extra = _apply_financing(result, financing)
    _apply_structural(result, structural)
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
    result.available_credit += _sum(facts, "undrawn_credit")
    # Samma amortering kan stå både i rapporten och i ett pressmeddelande: räkna den en gång.
    news_repayments = [e.get("amount") or 0.0 for e in result.financing_events
                       if e.get("type") == "repayment" and e.get("status") == "completed"]
    outflows = sum(f.value for f in facts if f.kind == "post_period_outflow" and f.value > 0
                   and not any(abs(f.value - r) <= 0.25 * max(f.value, r) for r in news_repayments))
    result.post_period_outflows = outflows
    result.reported_inflows_after_period = _sum(facts, "post_period_inflow")
    news_amounts = [e.get("amount") or 0.0 for e in result.financing_events if e.get("type") != "repayment"]
    inflows = sum(f.value for f in facts if f.kind == "post_period_inflow" and f.value > 0
                  and not any(abs(f.value - a) <= 0.25 * max(f.value, a) for a in news_amounts))
    if inflows:
        result.flags.append("post_period_inflows")
        extra += inflows
    if outflows:
        result.flags.append("post_period_outflows")
        extra -= outflows
    result.periods = _periods(facts, period_end)
    if not result.periods:
        result.status = "missing_cash_flow"
        return result
    chosen, recent = result.periods[-1], result.periods[0]
    result.ltm = _ltm(facts, result.periods, period_end)
    burn = result.ltm["monthly_burn"] if result.ltm else chosen.monthly_burn
    result.burn_basis = result.ltm["basis"] if result.ltm else "longest_period"
    if result.ltm and burn == 0 and chosen.monthly_burn > 0:
        # Tolv månader positiva men innevarande år bränner: nästan alltid en engångsinbetalning förra året.
        # Burn räknas då som snittet för innevarande år (längsta perioden), inte som noll.
        burn, result.burn_basis = chosen.monthly_burn, "longest_period"
        result.flags.append("ltm_one_off")
    current = max(chosen.monthly_burn, recent.monthly_burn)
    slower_recent = bool(result.ltm and burn > 0 and current > 0 and chosen.months >= 6
                         and burn > current * LTM_DISTORTED_RATIO)
    faster_recent = (len(result.periods) > 1 and recent.monthly_burn > 0
                     and recent.monthly_burn > burn * RECENT_RATIO)
    if chosen.investments_basis == "investing_cash_flow":
        result.flags.append("investing_as_capex")
    since = max(0.0, _months(period_end, today))
    result.months_since_report = round(since, 1)
    if since > STALE_MONTHS:
        result.flags.append("stale_report")
    result.monthly_burn = burn
    if faster_recent:
        result.flags.append("recent_period_burn_higher")
        result.recent_monthly_burn = recent.monthly_burn
        result.recent_runway_today = round(
            max(0.0, cash + extra - recent.monthly_burn * since) / recent.monthly_burn, 1)
    elif slower_recent:
        result.flags.append("ltm_distorted")
        result.recent_monthly_burn = current
        result.recent_runway_today = round(
            max(0.0, cash + extra + result.available_credit - current * since) / current, 1)
    if burn == 0:
        result.cash_flow_positive = True
        if recent.monthly_burn > 0:
            result.flags.append("recent_period_burning")   # positivt totalt, men senaste perioden bränner
        return result
    result.runway_at_report = round(cash / burn, 1)
    if (cash + extra + result.available_credit) / burn < since:
        # Kassan borde redan vara slut: nästan alltid ny finansiering efter rapporten.
        result.flags.append("cash_exhausted_since_report")
    cash_today = max(0.0, cash + extra - burn * since)
    result.cash_today = cash_today
    result.runway_cash_today = round(cash_today / burn, 1)
    # Dagens runway räknar in outnyttjad kreditlinje (den är tillgänglig pengar); kassan visas separat.
    result.runway_today = round((cash_today + result.available_credit) / burn, 1)
    _pro_forma(result, cash_today, burn)
    _credit_and_acute(result, cash_today)
    _debt_warning(result, facts, cash_today)
    return result
