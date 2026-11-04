"""Facit: verkliga bolag granskade mot rapportutdrag (tests/fixtures).

Granskningen (runway_facit_review.json) anger rätt månadsburn per bolag.
Motorn ska ligga inom 20 % för de fall som gick att verifiera.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capital_radar.runway_engine import Fact, Report, evaluate

FIXTURES = Path(__file__).parent / "fixtures"
CASES = {c["comparison"]["issuer_id"]: c for c in json.loads((FIXTURES / "runway_facit_cases.json").read_text(encoding="utf-8"))}
REVIEW = json.loads((FIXTURES / "runway_facit_review.json").read_text(encoding="utf-8"))
VERIFIED = [r for r in REVIEW if r["verdict"] in {"new", "both_ok", "both_wrong"}
            and (r.get("correct_monthly_burn_sek") is not None or r.get("cash_flow_positive"))]


def _report(case: dict) -> Report:
    meta = case["report"]
    facts = tuple(
        Fact(f["fact_type"], float(f["normalized_value"]), f["currency"], f["period_start"], f["period_end"],
             f["as_of_date"], float(f["confidence"] or 0), int(case["comparison"]["report_document_id"]), f["page"])
        for f in case["facts"] if f["normalized_value"] is not None)
    return Report(case["comparison"]["issuer_id"], int(case["comparison"]["report_document_id"]),
                  meta["period_start"], meta["period_end"], meta["period_months"], None, facts)


# Kända avvikelser där facit bygger på ett annat legitimt val eller där data saknas.
KNOWN_DEVIATIONS = {
    36: "Ferronordic: facit använder nettoinvesteringar (efter avyttringar); motorn bruttocapex.",
    88: "Sleep Cycle: aktiverad utveckling för 9M saknas i extraktionen (ny rapportkedja, steg 1b).",
    277: "Observit: facit osäkert mellan Q2 utan investeringar och H1 med investeringar.",
}


@pytest.mark.parametrize("review", VERIFIED, ids=lambda r: f"{r['issuer_id']}-{r['name']}")
def test_engine_matches_reviewed_burn(review):
    if review["issuer_id"] in KNOWN_DEVIATIONS:
        pytest.xfail(KNOWN_DEVIATIONS[review["issuer_id"]])
    result = evaluate(_report(CASES[review["issuer_id"]]), [], as_of="2026-11-04")
    if review.get("cash_flow_positive"):
        assert result.cash_flow_positive, result.assumptions
        return
    expected = float(review["correct_monthly_burn_sek"])
    assert result.monthly_burn is not None, result.status
    assert abs(result.monthly_burn - expected) <= 0.2 * expected, (
        f"motor {result.monthly_burn:,.0f} vs facit {expected:,.0f}; {review['burn_basis_period']}")
