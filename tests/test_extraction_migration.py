"""Tester för extraction quality-gate (V2).

Bevisar att quality_gate klassificerar accepterad output, output som kräver
mänsklig granskning och output som ska eskaleras till Pro. Dessa tester är
oberoende av provider och nätverk.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.extraction_gate import (
    GATE_ACCEPT,
    GATE_ESCALATE,
    GATE_REQUIRES_REVIEW,
    quality_gate,
)


def _fact(fact_type: str, *, confidence: float = 0.9, page: int = 5) -> dict:
    return {
        "fact_type": fact_type,
        "reported_value": -12.0,
        "normalized_value": -12_000_000.0,
        "currency": "SEK",
        "reported_unit": "millions",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "as_of_date": "2026-03-31",
        "evidence_label": "reported_fact",
        "source_page": page,
        "source_excerpt": f"Excerpt for {fact_type}",
        "confidence": confidence,
    }


def _extraction_payload(
    *,
    facts: list[dict] | None = None,
    events: list[dict] | None = None,
    uncertainties: list[str] | None = None,
    requires_human_review: bool = False,
) -> dict:
    return {
        "document_class": "financial_report",
        "document_summary": "Delårsrapport Q1 2026.",
        "report": {
            "is_financial_report": True,
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "publication_date": "2026-05-01",
            "currency": "SEK",
            "reported_scale": "millions",
            "period_months": 3,
            "confidence": 0.9,
            "facts": facts if facts is not None else [
                _fact("cash_and_cash_equivalents"),
                _fact("operating_cash_flow"),
            ],
        },
        "financing_events": events if events is not None else [],
        "bergs_fit": {
            "score": 50,
            "level": "medium",
            "mandate_type": "equity_financing",
            "rationale": "Negativt operativt kassaflöde dokumenterat.",
            "next_step": "Följ upp kommande kvartalsrapport.",
            "confidence": 0.6,
        },
        "quality": {
            "complete": True,
            "uncertainties": uncertainties if uncertainties is not None else [],
            "requires_human_review": requires_human_review,
        },
    }


class QualityGateTests(unittest.TestCase):
    def test_quality_gate_accepts_clean_output(self) -> None:
        decision, signals = quality_gate(_extraction_payload())
        self.assertEqual(decision, GATE_ACCEPT)
        self.assertEqual(signals, [])

    def test_quality_gate_requires_review_on_ambiguity(self) -> None:
        payload = _extraction_payload(
            uncertainties=[f"osäkerhet {i}" for i in range(7)]
        )
        decision, signals = quality_gate(payload)
        self.assertEqual(decision, GATE_REQUIRES_REVIEW)
        self.assertTrue(
            any(s.startswith("material_ambiguity") for s in signals)
        )

    def test_quality_gate_escalates_on_unresolved_financing(self) -> None:
        event = {
            "event_type": "rights_issue",
            "description": "Announced rights issue.",
            "announcement_date": "2026-04-15",
            "effective_date": None,
            "status": "announced",
            "cash_direction": "inflow",
            "gross_amount": 100_000_000.0,
            "net_amount": None,
            "cash_effect_amount": None,
            "currency": "SEK",
            "liquidity_effect_kind": "none",
            "committed_amount": None,
            "conditional_amount": None,
            "reflected_in_report": "no",
            "conditions": None,
            "use_of_proceeds": None,
            "evidence_label": "reported_fact",
            "source_page": 3,
            "source_excerpt": "The board resolved on a rights issue.",
            "confidence": 0.8,
        }
        payload = _extraction_payload(events=[event])
        decision, signals = quality_gate(payload)
        self.assertEqual(decision, GATE_ESCALATE)
        self.assertTrue(
            any(
                s.startswith("unresolved_financing_structure")
                for s in signals
            )
        )


if __name__ == "__main__":
    unittest.main()
