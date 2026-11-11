"""Markörer per bolag: tecken på kapitalbehov och sammanhang som hjälper bedömningen.

Tre nivåer:
- `capital`: uttalat kapitalbehov. Ger full poäng i prospektpoängen och placerar bolaget i sektionen
  "Uttalat kapitalbehov" på prospektsidan.
- `half`: indirekt kapitalbehov (lån som förfaller, brygglån, teckningsoptioner som snart löses).
  Ger halv poäng och placerar också bolaget i sektionen.
- `context`: visas och går att filtrera på, men påverkar inte poängen.

Signalerna ur texter kommer från plans.py; övriga markörer räknas fram ur data som redan finns
(runway, finansieringshändelser, finanskalendern och nyhetsrubriker). Inga AI-anrop.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from .calendar import next_report
from .plans import active_all

# Ordning = visningsordning i filter och detaljpanel. (nyckel, etikett, nivå)
MARKERS = (
    ("stamma_issue", "Stämmobeslut om emission", "capital"),
    ("egm_called", "Kallelse till extra stämma", "capital"),
    ("plan", "Planerad kapitalanskaffning", "capital"),
    ("going_concern", "Osäkerhet om fortsatt drift", "capital"),
    ("debt_due", "Lån förfaller före kassan räcker", "half"),
    ("bridge_loan", "Utestående brygglån", "half"),
    ("to_window", "TO-period inom 90 dagar", "half"),
    ("evaluating", "Utvärderar finansiering", "context"),
    ("weak_issue", "Svag senaste emission", "context"),
    ("report_soon", "Rapport inom 30 dagar", "context"),
    ("mgmt_change", "VD eller CFO lämnar", "context"),
    ("lp_ended", "Likviditetsgaranti avslutas", "context"),
)
LABEL = {key: label for key, label, _tier in MARKERS}
TIER = {key: tier for key, _label, tier in MARKERS}
ORDER = {key: i for i, (key, _label, _tier) in enumerate(MARKERS)}

BRIDGE_DAYS = 120
TO_DAYS = 90
WEAK_DAYS = 365
WEAK_RATE = 80.0
REPORT_DAYS = 30
NEWS_DAYS = 120
EQUITY = ("rights_issue", "directed_issue", "warrants", "convertible")
MGMT = re.compile(r"\b(?:vd|ceo|cfo|finanschef|verkställande\s+direktör)\b.{0,50}"
                  r"(?:lämnar|avgår|slutar|frånträder|resign|steps?\s+down|leav|depart)", re.I)
LP_ENDED = re.compile(r"(?:avslutar|säger\s+upp|upphör|terminat|ends?)\b.{0,40}(?:likviditetsgaranti|liquidity\s+(?:provider|guarantee))"
                      r"|(?:likviditetsgaranti|liquidity\s+(?:provider|guarantee)).{0,40}(?:avslutas|upphör|sägs\s+upp|terminat|ends?)", re.I)


def _marker(key: str, detail: str, day: str | None = None, url: str | None = None) -> dict:
    return {"key": key, "label": LABEL[key], "tier": TIER[key], "detail": detail, "date": day, "url": url}


def _msek(value: float | None, currency: str | None) -> str:
    if value is None:
        return "okänt belopp"
    return f"{value / 1e6:,.1f} M{(currency or 'SEK').upper()}".replace(",", " ").replace(".", ",")


def _raised_after(events: list[dict], day: str) -> bool:
    return any(e.get("type") in EQUITY and e.get("status") == "completed" and (e.get("event_date") or "")[:10] >= day
               for e in events)


def for_company(item: dict, signals: list[dict], events: list[dict], report: dict | None,
                titles: list[dict], today: str) -> list[dict]:
    """Markörer för ett bolag. `item` är en rad ur web.latest_rows, `signals` ur plans.active_all,
    `events` ur qualify.load_history, `report` ur calendar.next_report och `titles` nyhetsrubriker."""
    day = date.fromisoformat(today[:10])
    found: dict[str, dict] = {}
    for s in signals:
        if s["kind"] in LABEL:
            detail = s.get("quote") or ""
            if s.get("timing"):
                detail = f"{s['timing']}: {detail}"
            found.setdefault(s["kind"], _marker(s["kind"], detail, s.get("published_at"), s.get("url")))
    currency = item.get("currency")
    debt, cash = item.get("debt_due_12m"), item.get("cash_today", item.get("cash"))
    if debt and debt > 0 and cash is not None and debt > cash + (item.get("available_credit") or 0):
        found["debt_due"] = _marker("debt_due", f"Lån {_msek(debt, currency)} förfaller inom 12 mån; kassa idag "
                                                f"{_msek(cash, currency)}.", item.get("period_end"))
    bridge_cutoff = (day - timedelta(days=BRIDGE_DAYS)).isoformat()
    for e in events:
        when = (e.get("event_date") or "")[:10]
        if (e.get("type") == "bridge_loan" and e.get("status") in ("announced", "completed") and when >= bridge_cutoff
                and not _raised_after(events, when) and "bridge_loan" not in found):
            found["bridge_loan"] = _marker("bridge_loan", f"Brygglån {_msek(e.get('gross_amount'), e.get('currency'))} "
                                                          f"({when}); betalas oftast med en kommande emission.",
                                           when, e.get("url"))
    window_end = (day + timedelta(days=TO_DAYS)).isoformat()
    for e in events:
        start, end = (e.get("exercise_start") or "")[:10], (e.get("exercise_end") or "")[:10]
        if (e.get("type") == "warrants" and e.get("status") != "cancelled" and start and end
                and end >= today[:10] and start <= window_end and "to_window" not in found):
            found["to_window"] = _marker("to_window", f"Lösenperiod {start}–{end}.", start, e.get("url"))
    weak_cutoff = (day - timedelta(days=WEAK_DAYS)).isoformat()
    for e in sorted(events, key=lambda e: e.get("event_date") or "", reverse=True):
        rate = e.get("subscription_rate_pct")
        if (e.get("type") == "rights_issue" and e.get("status") == "completed" and rate is not None
                and rate < WEAK_RATE and (e.get("event_date") or "")[:10] >= weak_cutoff):
            found["weak_issue"] = _marker("weak_issue", f"Företrädesemission {(e.get('event_date') or '')[:10]} "
                                                        f"tecknades till {rate:g} %.", (e.get("event_date") or "")[:10],
                                          e.get("url"))
            break
    if report and report.get("date") and report["date"] <= (day + timedelta(days=REPORT_DAYS)).isoformat():
        found["report_soon"] = _marker("report_soon", f"Nästa rapport {report['date']}.", report["date"])
    for t in titles:   # nyast först
        if "mgmt_change" not in found and MGMT.search(t["title"]):
            found["mgmt_change"] = _marker("mgmt_change", t["title"], t["published_at"][:10], t["url"])
        if "lp_ended" not in found and LP_ENDED.search(t["title"]):
            found["lp_ended"] = _marker("lp_ended", t["title"], t["published_at"][:10], t["url"])
    return sorted(found.values(), key=lambda m: ORDER[m["key"]])


def attach(connection: sqlite3.Connection, items: list[dict], history: dict[int, list[dict]], today: str) -> None:
    """Lägg `markers` (lista) och `capital_signal` (viktigaste signalen, för bannern) på varje rad."""
    signals = active_all(connection, history, today)
    reports = next_report(connection, today)
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=NEWS_DAYS)).isoformat()
    titles: dict[int, list[dict]] = {}
    for row in connection.execute(
            """SELECT company_id, title, published_at, url FROM news WHERE substr(published_at, 1, 10) >= ?
               ORDER BY published_at DESC""", (cutoff,)):
        titles.setdefault(row["company_id"], []).append(dict(row))
    for item in items:
        company_signals = signals.get(item["id"], [])
        item["capital_signal"] = company_signals[0] if company_signals else None
        item["markers"] = for_company(item, company_signals, history.get(item["id"], []),
                                      reports.get(item["id"]), titles.get(item["id"], []), today)


def need_level(markers: list[dict]) -> str | None:
    """'capital' om något uttalat kapitalbehov finns, annars 'half' om ett indirekt finns, annars None."""
    tiers = {m["tier"] for m in markers or ()}
    return "capital" if "capital" in tiers else ("half" if "half" in tiers else None)
