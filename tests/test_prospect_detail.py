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
    assert len(listing) == 5  # avbrutna och händelser äldre än 24 månader visas inte
    assert listing[0]["date"] == "2026-10-20" and listing[-1]["date"] == "2025-06-01"


def test_advisers_are_parsed_from_extraction():
    raw = json.dumps({"is_financing": True, "unit": "MSEK", "currency": "SEK", "events": [
        {"type": "rights_issue", "status": "announced", "gross_amount": 50, "date": "2026-10-01",
         "quote": "företrädesemission", "advisers": "Carnegie; Redeye"}]})
    events = parse_financing(raw, "2026-10-01")
    assert events[0].advisers == "Carnegie; Redeye"
