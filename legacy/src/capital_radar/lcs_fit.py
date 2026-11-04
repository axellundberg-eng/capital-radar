"""Deterministisk LCS-fit (bergs_fit) för V2-extraktioner.

V2-extraktionen (extraction_models.CapitalNeedExtraction) producerar av design
INGET ``bergs_fit`` — det ska beräknas downstream. Denna modul är den
downstream-beräkningen: den konsumerar redan extraherade strukturerade fakta
(``report.facts``) och finansieringshändelser (``financing_events``) från det
lagrade ``parsed_output_json`` och härleder en LCS-fit-score helt
deterministiskt. Ingen AI, inga nya provider-anrop.

Scoren speglar samma semantik som legacy-flödet (0-100, högre = starkare
LCS-fit / mandate-möjlighet) men räknas fram från verifierbara inputs istället
för att gissas av en modell. Returnerar en dict med exakt de fält som
``_validate_bergs_fit`` och ``_save_interpretation`` förväntar sig.
"""

from __future__ import annotations

from typing import Any


_FACT_CASH = "cash_and_cash_equivalents"
_FACT_CFO = "operating_cash_flow"

_EQUITY_EVENTS = {"rights_issue", "directed_issue", "convertible", "warrant_exercise"}
_DEBT_EVENTS = {
    "loan_draw",
    "loan_commitment",
    "credit_facility",
    "bridge_loan",
    "debt_repayment",
}


def _fact_value(facts: list[dict[str, Any]], fact_type: str) -> float | None:
    for fact in facts:
        if fact.get("fact_type") != fact_type:
            continue
        value = fact.get("normalized_value")
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _runway_months(cash: float | None, cfo: float | None, period_months: float | None) -> float | None:
    if cash is None or cfo is None or not period_months or period_months <= 0:
        return None
    monthly_burn = -cfo / period_months
    if monthly_burn <= 0:
        return None
    return cash / monthly_burn


def compute_bergs_fit(parsed_output: dict[str, Any]) -> dict[str, Any]:
    """Beräkna bergs_fit deterministiskt från en V2-extraktion.

    Returnerar alltid en dict med samtliga obligatoriska fält. Om det inte
    finns tillräckligt underlag returneras ``not_assessable`` med score None
    (samma fallback-semantik som legacy).
    """
    not_assessable = {
        "score": None,
        "level": "not_assessable",
        "mandate_type": "not_assessable",
        "rationale": "Otillräckligt strukturerat underlag för LCS-fit.",
        "next_step": "Kör ingen ny AI-analys enbart för LCS-fit.",
        "confidence": 0.0,
    }
    if not isinstance(parsed_output, dict):
        return dict(not_assessable)

    report = parsed_output.get("report") or {}
    facts = report.get("facts") or []
    events = parsed_output.get("financing_events") or []
    if not isinstance(facts, list):
        facts = []
    if not isinstance(events, list):
        events = []

    cash = _fact_value(facts, _FACT_CASH)
    cfo = _fact_value(facts, _FACT_CFO)
    period_months = report.get("period_months")
    try:
        period_months = float(period_months) if period_months is not None else None
    except (TypeError, ValueError):
        period_months = None

    runway = _runway_months(cash, cfo, period_months)

    active_events = [
        e for e in events
        if isinstance(e, dict) and e.get("status") not in {"cancelled"}
    ]
    event_types = {str(e.get("event_type")) for e in active_events}
    has_equity = bool(event_types & _EQUITY_EVENTS)
    has_debt = bool(event_types & _DEBT_EVENTS)

    # Ingen meningsfull signal alls -> not_assessable.
    if runway is None and not active_events:
        return dict(not_assessable)

    # Runway-driven base score: kort runway => högt kapitalbehov => högre fit.
    if runway is None:
        base = 40  # burn ej beräkningsbar men det finns finansieringsaktivitet
    elif runway < 3:
        base = 90
    elif runway < 6:
        base = 75
    elif runway < 9:
        base = 60
    elif runway < 12:
        base = 45
    elif runway < 18:
        base = 30
    elif runway < 24:
        base = 18
    else:
        base = 8

    # Finansieringsaktivitet justerar uppåt (pågående/kommenderad kapitalanskaffning).
    if has_equity:
        base += 10
    elif has_debt:
        base += 6

    score = max(0, min(100, int(round(base))))

    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"

    if has_equity:
        mandate_type = "equity_financing"
    elif has_debt:
        mandate_type = "debt_financing"
    elif score >= 55:
        mandate_type = "strategic_financing"
    else:
        mandate_type = "monitoring"

    if runway is not None:
        rationale = (
            f"Beräknad runway {runway:.1f} månader från extraherad kassa och "
            f"operativt kassaflöde; {len(active_events)} finansieringshändelse(r) "
            f"beaktade."
        )
    else:
        rationale = (
            f"Runway ej beräkningsbar; bedömning baserad på "
            f"{len(active_events)} finansieringshändelse(r)."
        )

    confidence = 0.6 if runway is not None else 0.4

    return {
        "score": score,
        "level": level,
        "mandate_type": mandate_type,
        "rationale": rationale,
        "next_step": "Verifiera kassa, burn rate och finansieringsstatus manuellt.",
        "confidence": confidence,
    }
