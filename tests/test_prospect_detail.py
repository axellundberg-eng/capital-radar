"""Prospekt: emissioner de senaste 24 månaderna, finansieringshändelser och rådgivare."""

from __future__ import annotations

import json

from radar.financing import parse_financing
from radar.qualify import recent_financing

TODAY = "2026-11-05"


def row(kind, status, day, gross=10e6, advisers=None):
    return {"type": kind, "status": status, "gross_amount": gross, "net_amount": None, "currency": "SEK",
            "event_date": day, "quote": "citat", "drawn_amount": None, "advisers": advisers}


def test_recent_financing_counts_equity_rounds_once_and_skips_loans_and_old_events():
    rows = [row("rights_issue", "announced", "2026-10-01"), row("rights_issue", "completed", "2026-10-20"),
            row("directed_issue", "completed", "2025-06-01"), row("loan", "announced", "2026-09-01"),
            row("warrants", "completed", "2026-08-01"), row("rights_issue", "completed", "2024-01-01"),
            row("directed_issue", "cancelled", "2026-07-01")]
    count, listing = recent_financing(rows, TODAY)
    assert count == 2  # rätt-emissionen räknas en gång; lånet, teckningsoptionerna och den gamla emissionen inte
    assert len(listing) == 4  # samma rätt-emission (annonserad + genomförd) är en affär; avbrutna och gamla visas inte
    assert listing[0]["date"] == "2026-10-20" and listing[-1]["date"] == "2025-06-01"


def test_advisers_are_parsed_from_extraction():
    raw = json.dumps({"is_financing": True, "unit": "MSEK", "currency": "SEK", "events": [
        {"type": "rights_issue", "status": "announced", "gross_amount": 50, "date": "2026-10-01",
         "quote": "företrädesemission", "advisers": "Carnegie; Redeye"}]})
    events = parse_financing(raw, "2026-10-01")
    assert events[0].advisers == "Carnegie; Redeye"


def test_issue_terms_are_parsed_scaled_and_give_dilution():
    raw = json.dumps({"is_financing": True, "unit": "MSEK", "currency": "SEK", "events": [
        {"type": "rights_issue", "status": "completed", "gross_amount": 50, "date": "2026-10-01",
         "quote": "utfall", "subscription_commitments": 10, "guarantee_amount": 30, "guarantee_fee_pct": 12,
         "guarantors": "Fondbolaget; Investerare AB", "subscription_rate_pct": 87, "guarantee_utilized": 6.5,
         "issue_price": 1.25, "shares_issued": 40000000, "shares_before": 60000000, "discount_pct": 25}]})
    event = parse_financing(raw, "2026-10-01")[0]
    assert event.guarantee_amount == 30e6 and event.subscription_commitments == 10e6
    assert event.guarantee_utilized == 6.5e6 and event.issue_price == 1.25      # pris skalas inte med enheten
    assert event.shares_issued == 40e6 and event.guarantee_fee_pct == 12 and event.subscription_rate_pct == 87
    row = {"type": "rights_issue", "status": "completed", "gross_amount": 50e6, "net_amount": None,
           "currency": "SEK", "event_date": "2026-10-01", "quote": "utfall", "drawn_amount": None,
           "advisers": None, "shares_issued": 40e6, "shares_before": 60e6, "guarantee_amount": None}
    english = {**row, "guarantee_amount": 30e6, "advisers": "Carnegie"}
    _, listing = recent_financing([row, english], TODAY)
    assert len(listing) == 1 and listing[0]["dilution_pct"] == 40.0             # 40 / (60 + 40)
    assert listing[0]["guarantee_amount"] == 30e6 and listing[0]["advisers"] == "Carnegie"


def test_warrant_terms_are_parsed():
    raw = json.dumps({"is_financing": True, "unit": "SEK", "currency": "SEK", "events": [
        {"type": "warrants", "status": "announced", "gross_amount": None, "date": "2026-11-01", "quote": "TO2",
         "exercise_price": 0.85, "exercise_start": "2026-12-01", "exercise_end": "2026-12-15"}]})
    event = parse_financing(raw, "2026-11-01")[0]
    assert event.exercise_price == 0.85 and event.exercise_start == "2026-12-01" and event.exercise_end == "2026-12-15"


def test_round_steps_are_merged_into_one_deal():
    # Beslut, utfall och registrering (svenska + engelska) av samma företrädesemission blir en affär.
    rows = [row("rights_issue", "announced", "2026-09-03", 57.5e6), row("rights_issue", "announced", "2026-09-03", 58e6),
            row("rights_issue", "completed", "2026-10-01", 57.5e6), row("rights_issue", "completed", "2026-10-08", 57.5e6),
            row("loan", "completed", "2026-05-01", 5e6)]
    count, listing = recent_financing(rows, TODAY)
    assert count == 1 and len(listing) == 2
    deal = listing[0]
    assert deal["status"] == "completed" and deal["first_date"] == "2026-09-03" and deal["press_releases"] == 4
