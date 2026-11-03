"""Quality gate för finansiell extraktion (deterministisk eskalationslogik).

Denna modul äger trösklarna och beslutslogiken som avgör om en validerad
extraktionsoutput accepteras, eskaleras till Pro eller kräver manuell
granskning. Eskalation till Pro sker ENBART på explicita maskinsignaler —
aldrig som default.
"""

from __future__ import annotations

from typing import Any, Mapping

EXTRACTION_AGENT_TYPE = "financial_extraction"
EXTRACTION_SCHEMA_NAME = "capital_need_extraction"

# Quality-gate-trösklar — konservativa placeholders som benchmark-agenten
# kalibrerar senare. Eskalation till Pro ska vara undantaget, inte regeln.
MIN_MEAN_FACT_CONFIDENCE = 0.45
MIN_EVIDENCE_COMPLETENESS = 0.5
MAX_UNCERTAINTIES_BEFORE_REVIEW = 6

# Faktatyper som anses centrala för kapitalbehovsbilden.
_CENTRAL_FACT_TYPES = frozenset(
    {"cash_and_cash_equivalents", "operating_cash_flow"}
)

# Beslut från quality-gate:n.
GATE_ACCEPT = "accept"
GATE_ESCALATE = "escalate"
GATE_REQUIRES_REVIEW = "requires_review"


def quality_gate(parsed: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Deterministisk quality gate för validerad extraktionsoutput.

    Returnerar ``(beslut, signaler)`` där beslut är ett av
    ``accept`` / ``escalate`` / ``requires_review``. Eskalation sker bara
    på explicita maskinsignaler — aldrig som default.
    """
    signals: list[str] = []
    report = parsed.get("report") or {}
    quality = parsed.get("quality") or {}
    facts = report.get("facts") or []
    events = parsed.get("financing_events") or []

    # Signal 1: centrala fakta saknas trots att dokumentet borde ha dem.
    present_types = {
        fact.get("fact_type") for fact in facts if isinstance(fact, Mapping)
    }
    missing_central = _CENTRAL_FACT_TYPES - present_types
    if missing_central:
        signals.append(
            "central_facts_missing:" + ",".join(sorted(missing_central))
        )

    # Signal 2: låg genomsnittlig confidence på fakta/händelser.
    confidences = [
        float(item["confidence"])
        for item in (*facts, *events)
        if isinstance(item, Mapping)
        and isinstance(item.get("confidence"), (int, float))
    ]
    if confidences:
        mean_conf = sum(confidences) / len(confidences)
        if mean_conf < MIN_MEAN_FACT_CONFIDENCE:
            signals.append(f"low_mean_confidence:{mean_conf:.2f}")

    # Signal 3: låg evidence completeness (faktarader utan sidhänvisning).
    if facts:
        with_page = sum(
            1
            for fact in facts
            if isinstance(fact, Mapping) and fact.get("source_page")
        )
        completeness = with_page / len(facts)
        if completeness < MIN_EVIDENCE_COMPLETENESS:
            signals.append(f"low_evidence_completeness:{completeness:.2f}")

    # Signal 4: olösta finansieringsstrukturer — annonserade/godkända/
    # villkorade händelser utan bekräftad cash-effekt.
    unresolved = [
        event
        for event in events
        if isinstance(event, Mapping)
        and event.get("cash_effect_amount") is None
        and event.get("status") in {"announced", "approved", "conditional"}
    ]
    if unresolved:
        signals.append(f"unresolved_financing_structure:{len(unresolved)}")

    # Signal 5: många osäkerheter => materiell tvetydighet.
    uncertainties = quality.get("uncertainties") or []
    if len(uncertainties) > MAX_UNCERTAINTIES_BEFORE_REVIEW:
        signals.append(f"material_ambiguity:{len(uncertainties)}")

    # Signal 6: modellen flaggar själv att granskning krävs.
    if quality.get("requires_human_review") is True:
        signals.append("model_requires_review")

    escalate_signals = {
        "central_facts_missing",
        "low_mean_confidence",
        "low_evidence_completeness",
        "unresolved_financing_structure",
    }
    fired = {signal.split(":", 1)[0] for signal in signals}
    if fired & escalate_signals:
        return GATE_ESCALATE, signals
    if signals:
        return GATE_REQUIRES_REVIEW, signals
    return GATE_ACCEPT, signals


__all__ = [
    "EXTRACTION_AGENT_TYPE",
    "EXTRACTION_SCHEMA_NAME",
    "GATE_ACCEPT",
    "GATE_ESCALATE",
    "GATE_REQUIRES_REVIEW",
    "MAX_UNCERTAINTIES_BEFORE_REVIEW",
    "MIN_EVIDENCE_COMPLETENESS",
    "MIN_MEAN_FACT_CONFIDENCE",
    "quality_gate",
]
