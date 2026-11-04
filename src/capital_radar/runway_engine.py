"""Runway-motor v1: en enda, förklarbar beräkning per bolag.

Ersätter på sikt de två parallella spåren i `runway.py` (assessment) och
`radar_analysis.py` (pro forma). Körs först i skuggläge bredvid dem.

Regler:
- Kassa = `cash_and_cash_equivalents` per rapportens periodslut.
- Burn räknas per fakta utifrån faktans *egen* period (kvartal, H1, YTD,
  helår), inte rapportens. Finns flera perioder väljs den högsta burnen
  (försiktigt antagande) och alla redovisas.
- Investeringar (`capex`) för samma period läggs till burnen.
- Kassan rullas fram med burn från periodslut till `as_of`.
- Säker runway räknar bara genomförd finansiering efter rapporten.
  Förväntad runway lägger till annonserad finansiering.
- Saknade fakta ger en status, aldrig ett tyst bortfall.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable

ENGINE_VERSION = "runway-engine-v1"
DAYS_PER_MONTH = 30.4375
STALE_AFTER_MONTHS = 7.0
CONFIRMED_STATUSES = frozenset({"completed", "registered", "preliminary_outcome"})
EXPECTED_STATUSES = frozenset({"announced", "approved", "subscription_period", "conditional", "proposed", "updated"})
CASH_FACT = "cash_and_cash_equivalents"
CFO_FACT = "operating_cash_flow"
CAPEX_FACT = "capex"


@dataclass(frozen=True)
class Fact:
    fact_type: str
    value: float
    currency: str | None
    period_start: str | None
    period_end: str | None
    as_of_date: str | None
    confidence: float
    document_id: int
    source_page: int | None = None


@dataclass(frozen=True)
class Report:
    issuer_id: int
    document_id: int
    period_start: str | None
    period_end: str
    period_months: float | None
    currency: str | None
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class FinancingEvent:
    transaction_id: str
    status: str | None
    amount: float | None
    currency: str | None
    announcement_date: str | None
    excluded_reason: str | None = None


@dataclass
class BurnCandidate:
    period_start: str
    period_end: str
    months: float
    operating_cash_flow: float
    capex: float
    monthly_burn: float
    document_id: int


@dataclass
class RunwayResult:
    issuer_id: int
    status: str
    engine_version: str = ENGINE_VERSION
    report_document_id: int | None = None
    report_period_end: str | None = None
    currency: str | None = None
    months_since_report: float | None = None
    cash_at_report: float | None = None
    monthly_burn: float | None = None
    cash_flow_positive: bool = False
    estimated_cash_today: float | None = None
    confirmed_financing: float = 0.0
    expected_financing: float = 0.0
    runway_at_report_months: float | None = None
    safe_runway_months: float | None = None
    expected_runway_months: float | None = None
    burn_candidates: list[BurnCandidate] = field(default_factory=list)
    included_events: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def months_between(start: str | None, end: str | None) -> float | None:
    try:
        days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    except (TypeError, ValueError):
        return None
    return days / DAYS_PER_MONTH


def period_months(start: str | None, end: str | None) -> float | None:
    """Längd i hela månader för en räkenskapsperiod (1 jan–31 mar = 3)."""
    span = months_between(start, end)
    if span is None:
        return None
    months = round(span + 1 / DAYS_PER_MONTH)
    return float(months) if 1 <= months <= 12 else None


def _best(facts: Iterable[Fact]) -> Fact | None:
    return max(facts, key=lambda fact: fact.confidence, default=None)


def _burn_candidates(report: Report, assumptions: list[str]) -> list[BurnCandidate]:
    cfo_facts = [f for f in report.facts if f.fact_type == CFO_FACT and f.period_end == report.period_end]
    capex_facts = [f for f in report.facts if f.fact_type == CAPEX_FACT and f.period_end == report.period_end]
    by_period: dict[tuple[str, str], Fact] = {}
    for fact in cfo_facts:
        start = fact.period_start
        if not start:
            start = report.period_start
            if start:
                assumptions.append("Kassaflödets startdatum saknas; rapportens period används.")
        if not start:
            continue
        key = (start, fact.period_end or report.period_end)
        current = by_period.get(key)
        if current is None or fact.confidence > current.confidence:
            by_period[key] = fact
    candidates = []
    for (start, end), fact in by_period.items():
        months = period_months(start, end)
        if months is None:
            continue
        capex_fact = _best(f for f in capex_facts if (f.period_start or start) == start)
        capex = abs(capex_fact.value) if capex_fact else 0.0
        burn = max(0.0, (-fact.value + capex) / months)
        candidates.append(BurnCandidate(start, end, months, fact.value, capex, burn, fact.document_id))
    return sorted(candidates, key=lambda c: c.months)


def evaluate(report: Report | None, events: Iterable[FinancingEvent], *, as_of: str,
             issuer_id: int | None = None) -> RunwayResult:
    if report is None:
        return RunwayResult(issuer_id=int(issuer_id or 0), status="missing_report",
                            assumptions=["Ingen finansiell rapport med extraherade fakta."])
    result = RunwayResult(issuer_id=report.issuer_id, status="ok", report_document_id=report.document_id,
                          report_period_end=report.period_end, currency=report.currency)
    assumptions = result.assumptions
    cash_fact = _best(f for f in report.facts if f.fact_type == CASH_FACT
                      and (f.as_of_date or f.period_end) == report.period_end)
    if cash_fact is None:
        result.status = "missing_cash"
        assumptions.append("Kassa per periodslut saknas i rapporten.")
        return result
    if cash_fact.currency and report.currency and cash_fact.currency != report.currency:
        result.flags.append("currency_mismatch")
    result.cash_at_report = cash_fact.value
    candidates = _burn_candidates(report, assumptions)
    result.burn_candidates = candidates
    if not candidates:
        result.status = "missing_cash_flow"
        assumptions.append("Operativt kassaflöde med känd period saknas.")
        return result
    burns = [c.monthly_burn for c in candidates]
    burn = max(burns)
    if len(candidates) > 1:
        assumptions.append(
            "Flera perioder finns (" + ", ".join(f"{c.months:g} mån" for c in candidates)
            + "); högsta burn används.")
        positive_burns = [b for b in burns if b > 0]
        if positive_burns and min(positive_burns) > 0 and max(positive_burns) / min(positive_burns) > 2:
            result.flags.append("burn_periods_diverge")
    if not any(c.capex for c in candidates):
        assumptions.append("Capex saknas för perioden; burn är bara operativt kassaflöde.")
    since = months_between(report.period_end, as_of) or 0.0
    since = max(0.0, since)
    result.months_since_report = round(since, 1)
    if since > STALE_AFTER_MONTHS:
        result.flags.append("stale_report")
        assumptions.append(f"Senaste rapporten är {since:.1f} mån gammal; nyare rapport bör finnas.".replace(".", ",", 1))
    if burn <= 0:
        result.cash_flow_positive = True
        result.monthly_burn = 0.0
        result.estimated_cash_today = cash_fact.value
        assumptions.append("Positivt kassaflöde i alla perioder; ingen burn.")
        _apply_events(result, events, report)
        return result
    result.monthly_burn = burn
    result.runway_at_report_months = round(cash_fact.value / burn, 1)
    result.estimated_cash_today = cash_fact.value - burn * since
    _apply_events(result, events, report)
    base = result.estimated_cash_today + result.confirmed_financing
    result.safe_runway_months = round(max(0.0, base) / burn, 1)
    result.expected_runway_months = round(max(0.0, base + result.expected_financing) / burn, 1)
    if result.estimated_cash_today < 0 and not result.confirmed_financing:
        result.flags.append("cash_likely_exhausted")
    return result


def _apply_events(result: RunwayResult, events: Iterable[FinancingEvent], report: Report) -> None:
    for event in events:
        if event.excluded_reason or event.amount is None or event.amount <= 0:
            continue
        if not event.announcement_date or event.announcement_date[:10] <= report.period_end:
            continue
        if event.currency and report.currency and event.currency != report.currency:
            continue
        if event.status in CONFIRMED_STATUSES:
            kind = "confirmed"
            result.confirmed_financing += event.amount
        elif event.status in EXPECTED_STATUSES:
            kind = "expected"
            result.expected_financing += event.amount
        else:
            continue
        result.included_events.append({"transaction_id": event.transaction_id, "kind": kind,
                                       "status": event.status, "amount": event.amount})
    if result.included_events:
        result.assumptions.append("Finansiering efter rapporten räknas brutto, före emissionskostnader.")


# --- Inläsning från databasen ------------------------------------------------

def load_reports(connection: sqlite3.Connection, issuer_ids: list[int] | None = None) -> dict[int, Report]:
    """Senaste finansiella rapport per bolag, med alla dess fakta."""
    where = ""
    params: list[Any] = []
    if issuer_ids is not None:
        where = f" AND r.issuer_id IN ({','.join('?' for _ in issuer_ids)})"
        params = list(issuer_ids)
    rows = connection.execute(
        f"""
        SELECT r.issuer_id, r.document_id, r.report_period_start, r.report_period_end,
               r.period_months, r.currency
        FROM report_extractions r
        WHERE r.is_financial_report=1 AND r.report_period_end IS NOT NULL{where}
          AND EXISTS (SELECT 1 FROM financial_facts f WHERE f.document_id=r.document_id)
        ORDER BY r.issuer_id, r.report_period_end DESC, r.document_id DESC
        """, params).fetchall()
    latest: dict[int, tuple] = {}
    for row in rows:
        latest.setdefault(int(row[0]), tuple(row))
    reports: dict[int, Report] = {}
    for issuer_id, (_, document_id, start, end, months, currency) in latest.items():
        facts = tuple(
            Fact(fact_type=f[0], value=float(f[1]), currency=f[2], period_start=f[3], period_end=f[4],
                 as_of_date=f[5], confidence=float(f[6] or 0), document_id=int(document_id), source_page=f[7])
            for f in connection.execute(
                """SELECT fact_type, normalized_value, currency, period_start, period_end,
                          as_of_date, confidence, source_page
                   FROM financial_facts WHERE document_id=? AND normalized_value IS NOT NULL""",
                (document_id,)).fetchall())
        reports[issuer_id] = Report(issuer_id, int(document_id), start, end, months, currency, facts)
    return reports


def events_from_pro_forma(analysis_items: Iterable[dict[str, Any]]) -> dict[int, list[FinancingEvent]]:
    """Återanvänd den deduplicerade affärslistan från pro forma-beräkningen."""
    by_issuer: dict[int, list[FinancingEvent]] = {}
    for item in analysis_items:
        for event in item.get("events") or []:
            by_issuer.setdefault(int(item["issuer_id"]), []).append(FinancingEvent(
                transaction_id=str(event.get("transaction_id")), status=event.get("status"),
                amount=event.get("amount"), currency=event.get("currency"),
                announcement_date=event.get("announcement_date"),
                excluded_reason=event.get("exclusion_reason")))
    return by_issuer
