"""Skuggjämförelse: runway-motorn mot nuvarande pro forma-beräkning.

    python -m capital_radar.runway_compare [--json utfil] [--issuer ID ...]

Läser databasen skrivskyddat och skriver en sammanfattning till stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date

from .config import Settings
from .radar_analysis import current_analysis
from .runway_engine import evaluate, events_from_pro_forma, load_reports


def compare(connection: sqlite3.Connection, issuer_ids: list[int] | None = None,
            *, as_of: str | None = None) -> list[dict]:
    as_of = as_of or date.today().isoformat()
    connection.execute("BEGIN")
    analysis = current_analysis(connection, issuer_ids)
    reports = load_reports(connection, issuer_ids)
    events = events_from_pro_forma(analysis)
    old_by_issuer = {int(item["issuer_id"]): item for item in analysis}
    names = dict(connection.execute("SELECT id, canonical_name FROM issuers").fetchall())
    rows = []
    for issuer_id in sorted(set(reports) | set(old_by_issuer)):
        new = evaluate(reports.get(issuer_id), events.get(issuer_id, []), as_of=as_of, issuer_id=issuer_id)
        old = old_by_issuer.get(issuer_id, {})
        rows.append({
            "issuer_id": issuer_id, "name": names.get(issuer_id),
            "status": new.status, "flags": new.flags,
            "old_reported": old.get("reported_runway_months"),
            "old_today": old.get("adjusted_runway_months"),
            "new_at_report": new.runway_at_report_months,
            "new_safe": new.safe_runway_months,
            "new_expected": new.expected_runway_months,
            "old_burn": old.get("monthly_burn"), "new_burn": new.monthly_burn,
            "months_since_report": new.months_since_report,
            "cash_flow_positive_old": old.get("cash_flow_positive"),
            "cash_flow_positive_new": new.cash_flow_positive,
            "burn_periods": [c.months for c in new.burn_candidates],
            "report_document_id": new.report_document_id,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    def differs(row, tolerance=1.0):
        old, new = row["old_today"], row["new_safe"]
        return old is not None and new is not None and abs(old - new) > tolerance
    burn_changed = [r for r in rows if r["old_burn"] and r["new_burn"] is not None
                    and abs(r["new_burn"] - r["old_burn"]) / r["old_burn"] > 0.1]
    return {
        "issuers": len(rows),
        "status": dict(Counter(r["status"] for r in rows)),
        "flags": dict(Counter(flag for r in rows for flag in r["flags"])),
        "runway_differs_over_1_month": sum(1 for r in rows if differs(r)),
        "burn_changed_over_10_pct": len(burn_changed),
        "under_6_old": sum(1 for r in rows if r["old_today"] is not None and r["old_today"] < 6),
        "under_6_new_safe": sum(1 for r in rows if r["new_safe"] is not None and r["new_safe"] < 6),
        "cash_flow_positive_old": sum(1 for r in rows if r["cash_flow_positive_old"]),
        "cash_flow_positive_new": sum(1 for r in rows if r["cash_flow_positive_new"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="skriv alla rader som JSON till fil")
    parser.add_argument("--issuer", type=int, action="append")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    uri = f"file:{settings.db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        rows = compare(connection, args.issuer)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=1, default=str)
    json.dump(summarize(rows), sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
