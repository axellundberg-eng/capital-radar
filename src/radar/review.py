"""Granskning: rimlighetskontroller, granskningskö och rättelser.

Kontrollerna körs utan AI och hittar troliga fel i det som extraherats och räknats fram. De sorterar
granskningskön så att en granskare (en chattsession varje måndag, se agents/radar-reviewer.md) börjar
med de troligaste felen. Rättelser sparas i tabellen `corrections` och läggs ovanpå extraherade fakta
och händelser vid varje omräkning, så att en ny extraktion inte skriver över dem.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

# Kontroll -> (allvar 1-3, beskrivning). Högre allvar granskas först.
CHECKS = {
    "cash_column": (3, "Kassan matchar inte första kolumnen i citatet: troligen fel period eller jämförelsekolumn"),
    "flow_same_value": (3, "Samma kassaflöde för två olika perioder: troligen samma kolumn läst två gånger"),
    "positive_but_burning": (3, "Markerat kassaflödespositivt men någon period visar burn"),
    "currency_mix": (2, "Finansieringshändelse i annan valuta än rapporten: räknas inte i runway"),
    "burn_jump": (2, "Burn skiljer mer än 3 gånger mellan perioderna"),
    "odd_period": (2, "Periodlängden är inte 3, 6, 9 eller 12 månader"),
    "unit_suspect": (1, "Kassa eller burn är mycket liten eller stor: kontrollera enheten (SEK/TSEK/MSEK)"),
    "duplicate_event": (1, "Samma finansiering från två pressmeddelanden (oftast svensk och engelsk version)"),
    "runway_extreme": (1, "Runway över 60 månader trots burn: kontrollera kassa och burn"),
    "exhausted_unexplained": (2, "Kassan borde vara slut men ingen finansiering har hittats efter rapporten"),
    "stale_report": (1, "Senaste rapporten är äldre än 7 månader: nyare rapport saknas eller är inte läst"),
    "missing_runway": (1, "Rapport finns men runway kunde inte räknas"),
}
# Tusentalsformatet kräver minst en grupp, så att årtal som 2026 läses hela (och filtreras bort) och inte
# delas upp i fragment som 202 och 6.
NUMBER = re.compile(r"-?\(?\d{1,3}(?:[  ,.]\d{3}(?!\d))+(?:[.,]\d+)?\)?|-?\(?\d+(?:[.,]\d+)?\)?")
SCALES = (1.0, 1e3, 1e6)

LATEST = """
SELECT c.id AS company_id, c.name, r.document_id, r.published_at, rr.result_json
FROM companies c JOIN reports r ON r.company_id = c.id
JOIN runway_results rr ON rr.document_id = r.document_id
WHERE r.document_id = (SELECT r2.document_id FROM reports r2 JOIN runway_results x ON x.document_id = r2.document_id
                       WHERE r2.company_id = c.id ORDER BY r2.published_at DESC, r2.document_id ASC LIMIT 1)
"""


@dataclass
class Finding:
    company_id: int
    name: str
    document_id: int
    check: str
    severity: int
    detail: str


def _numbers(quote: str | None) -> list[float]:
    """Talen i ett citat, i ordning. Parentes och minus blir negativt; tusentalsavgränsare tas bort."""
    values = []
    for token in NUMBER.findall(quote or ""):
        negative = token.startswith("-") or token.startswith("(")
        digits = token.strip("-()").replace(" ", " ")
        if re.fullmatch(r"\d{1,3}([ ,.]\d{3})+", digits):
            digits = re.sub(r"[ ,.]", "", digits)
        else:
            digits = digits.replace(" ", "").replace(",", ".")
        try:
            value = float(digits)
        except ValueError:
            continue
        values.append(-value if negative else value)
    return values


def _matches(value: float, number: float) -> bool:
    return any(number and abs(abs(value) - abs(number) * scale) <= 0.01 * abs(value) for scale in SCALES)


def column_mismatch(value: float, quote: str | None) -> str | None:
    """Om värdet matchar ett senare tal i citatet än det första som matchar någon skala: beskrivning, annars None.

    Citaten från tabeller listar ofta perioder i kolumner (aktuell period först). Ett värde som bara
    matchar en senare kolumn är troligen hämtat från jämförelseperioden."""
    numbers = [n for n in _numbers(quote) if abs(n) >= 10 and not 1900 <= abs(n) <= 2100]
    # Ett litet heltal först följt av minst två belopp är ett notnummer ("Kassa och bank 11 20 630 35 765").
    if len(numbers) >= 3 and numbers[0] == int(numbers[0]) and 0 < numbers[0] < 100:
        numbers = numbers[1:]
    if len(numbers) < 2 or not value:
        return None
    hits = [i for i, n in enumerate(numbers) if _matches(value, n)]
    if not hits or hits[0] == 0:
        return None
    return f"värdet {value:,.0f} matchar kolumn {hits[0] + 1} i citatet, första kolumnen är {numbers[0]:,.0f}"


def _latest(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(LATEST).fetchall()


def _facts(connection: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT f.* FROM facts f WHERE f.extraction_id = (SELECT id FROM extractions WHERE document_id=?
           AND status='ok' ORDER BY id DESC LIMIT 1)""", (document_id,)).fetchall()


def check_company(row: sqlite3.Row, facts: list[sqlite3.Row], events: list[sqlite3.Row],
                  news_after: int, today: str) -> list[Finding]:
    result = json.loads(row["result_json"])
    found: list[Finding] = []

    def add(check: str, detail: str) -> None:
        found.append(Finding(row["company_id"], row["name"], row["document_id"], check, CHECKS[check][0], detail))

    if result.get("status") != "ok":
        add("missing_runway", f"status {result.get('status')}")
        return found
    period_end = result.get("period_end") or ""
    for fact in facts:
        if fact["kind"] == "cash" and fact["period_end"] == period_end:
            problem = column_mismatch(fact["value"], fact["quote"])
            if problem:
                add("cash_column", problem)
    flows = [f for f in facts if f["kind"] == "operating_cash_flow" and f["value"]]
    for i, a in enumerate(flows):
        for b in flows[i + 1:]:
            if a["value"] == b["value"] and (a["period_start"], a["period_end"]) != (b["period_start"], b["period_end"]):
                add("flow_same_value", f"{a['value']:,.0f} för både {a['period_start']}–{a['period_end']} "
                                       f"och {b['period_start']}–{b['period_end']}")
    cash, burn = result.get("cash"), result.get("monthly_burn")
    if cash is not None and (0 < cash < 200_000 or cash > 20e9):
        add("unit_suspect", f"kassa {cash:,.0f}")
    if burn and (burn < 20_000 or burn > 2e9):
        add("unit_suspect", f"burn {burn:,.0f} per månad")
    periods = result.get("periods") or []
    if result.get("cash_flow_positive") and any((p.get("monthly_burn") or 0) > 0 for p in periods):
        add("positive_but_burning", ", ".join(f"{p['start']} ({p['months']} mån): burn {p['monthly_burn']:,.0f}"
                                              for p in periods if (p.get("monthly_burn") or 0) > 0))
    burns = [p["monthly_burn"] for p in periods if (p.get("monthly_burn") or 0) > 0]
    if len(burns) >= 2 and max(burns) > 3 * min(burns):
        add("burn_jump", f"burn per månad mellan {min(burns):,.0f} och {max(burns):,.0f}")
    odd = [p for p in periods if p.get("months") not in (3, 6, 9, 12)]
    if odd:
        add("odd_period", ", ".join(f"{p['start']}: {p['months']} mån" for p in odd))
    if not result.get("cash_flow_positive") and (result.get("runway_at_report") or 0) > 60:
        add("runway_extreme", f"{result['runway_at_report']} mån vid rapporten")
    if "cash_exhausted_since_report" in result.get("flags", []) and not result.get("financing_events"):
        add("exhausted_unexplained", f"{news_after} nyheter efter rapporten, ingen finansiering funnen")
    months = result.get("months_since_report") or 0
    if months > 7:
        add("stale_report", f"{months} mån sedan periodslut {period_end}")
    report_currency = next((f["currency"] for f in facts if f["currency"]), None)
    for event in events:
        if report_currency and event["currency"] and event["currency"] != report_currency \
                and (event["event_date"] or "")[:10] > period_end:
            add("currency_mix", f"{event['type']} {event['event_date']} i {event['currency']}, rapporten i {report_currency}")
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if a["type"] == b["type"] and a["status"] == b["status"] and a["news_document_id"] != b["news_document_id"] \
                    and a["gross_amount"] and b["gross_amount"] \
                    and abs(a["gross_amount"] - b["gross_amount"]) <= 0.05 * max(a["gross_amount"], b["gross_amount"]) \
                    and abs((date.fromisoformat(a["event_date"][:10]) - date.fromisoformat(b["event_date"][:10])).days) <= 14:
                add("duplicate_event", f"{a['type']} {a['status']} {a['gross_amount']:,.0f} den {a['event_date'][:10]} "
                                       f"och {b['event_date'][:10]} (händelse {a['id']} och {b['id']})")
    return found


def run_checks(connection: sqlite3.Connection, today: str | None = None,
               company_ids: set[int] | None = None) -> list[Finding]:
    today = today or date.today().isoformat()
    findings: list[Finding] = []
    for row in _latest(connection):
        if company_ids is not None and row["company_id"] not in company_ids:
            continue
        result = json.loads(row["result_json"])
        period_end = result.get("period_end") or "0000"
        events = connection.execute(
            "SELECT * FROM financing_events WHERE company_id=? AND status != 'cancelled' ORDER BY event_date",
            (row["company_id"],)).fetchall()
        news_after = connection.execute(
            "SELECT COUNT(*) FROM news WHERE company_id=? AND substr(published_at,1,10) > ?",
            (row["company_id"], period_end)).fetchone()[0]
        findings.extend(check_company(row, _facts(connection, row["document_id"]), events, news_after, today))
    findings.sort(key=lambda f: (-f.severity, f.name.casefold(), f.check))
    return findings


def summary(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.check] = counts.get(f.check, 0) + 1
    return {"findings": len(findings), "companies": len({f.company_id for f in findings}),
            "by_check": dict(sorted(counts.items(), key=lambda kv: (-CHECKS[kv[0]][0], kv[0])))}


def as_dicts(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def since_default(connection: sqlite3.Connection) -> str:
    """Datum för förra granskningen, eller sju dagar bakåt om ingen finns."""
    row = connection.execute("SELECT MAX(finished_at) FROM review_runs").fetchone()
    if row and row[0]:
        return row[0][:10]
    return (date.today() - timedelta(days=7)).isoformat()


# --- Rättelser -------------------------------------------------------------------------------------
# target 'fact': document_id + kind + period_start/period_end anger faktumet; field 'value' (nytt värde,
#   läggs till om faktumet saknas), 'delete' (tas bort) eller 'report_period_end' (rapportens periodslut).
# target 'event': event_id anger händelsen; field 'hide', 'status', 'type', 'gross_amount', 'currency',
#   'event_date' eller 'advisers'.
FACT_FIELDS = ("value", "delete", "report_period_end", "scale")
EVENT_FIELDS = ("hide", "status", "type", "gross_amount", "currency", "event_date", "advisers")


def active_corrections(connection: sqlite3.Connection, target: str, **where) -> list[sqlite3.Row]:
    sql = "SELECT * FROM corrections WHERE target=? AND reverted_at IS NULL"
    params: list = [target]
    for column, value in where.items():
        sql += f" AND {column}=?"
        params.append(value)
    return connection.execute(sql + " ORDER BY id", params).fetchall()


def apply_fact_corrections(connection: sqlite3.Connection, document_id: int, facts: list, period_end: str):
    """Lägg rättelser ovanpå extraherade fakta. Returnerar (fakta, periodslut)."""
    from .runway import Fact

    facts = list(facts)
    for c in active_corrections(connection, "fact", document_id=document_id):
        if c["field"] == "report_period_end":
            period_end = c["new_value"]
            continue
        if c["field"] == "scale":
            # Hela rapporten i fel enhet (t.ex. TSEK lästes som SEK): alla belopp multipliceras med faktorn.
            factor = float(c["new_value"])
            facts = [Fact(f.kind, f.value * factor, f.period_start, f.period_end) for f in facts]
            continue

        def same(f, c=c) -> bool:
            return f.kind == c["kind"] and (c["period_end"] is None or f.period_end == c["period_end"]) \
                and (c["period_start"] is None or f.period_start == c["period_start"])

        if c["field"] == "delete":
            facts = [f for f in facts if not same(f)]
        elif c["field"] == "value":
            value = float(c["new_value"])
            if any(same(f) for f in facts):
                facts = [Fact(f.kind, value, f.period_start, f.period_end) if same(f) else f for f in facts]
            else:
                facts.append(Fact(c["kind"], value, c["period_start"], c["period_end"] or period_end))
    return facts, period_end


def event_overrides(connection: sqlite3.Connection, company_id: int | None = None) -> dict[int, dict]:
    """event_id -> {fält: nytt värde}; 'hide' betyder att händelsen inte ska räknas eller visas."""
    where = {"company_id": company_id} if company_id is not None else {}
    overrides: dict[int, dict] = {}
    for c in active_corrections(connection, "event", **where):
        value = c["new_value"]
        if c["field"] == "gross_amount":
            value = float(value)
        overrides.setdefault(c["event_id"], {})[c["field"]] = value
    return overrides


def apply_event_row(row: dict, override: dict | None) -> dict | None:
    """En händelserad (dict med databasens kolumnnamn) med rättelser, eller None om den är dold."""
    if not override:
        return row
    if override.get("hide"):
        return None
    return {**row, **{k: v for k, v in override.items() if k != "hide"}}


def add_correction(connection: sqlite3.Connection, *, target: str, field: str, reason: str,
                   company_id: int, new_value=None, document_id: int | None = None,
                   event_id: int | None = None, kind: str | None = None, period_start: str | None = None,
                   period_end: str | None = None, author: str = "granskning") -> int:
    allowed = FACT_FIELDS if target == "fact" else EVENT_FIELDS
    if field not in allowed:
        raise ValueError(f"fältet {field} kan inte rättas för {target}; tillåtna: {', '.join(allowed)}")
    if not reason or not reason.strip():
        raise ValueError("en rättelse kräver en motivering")
    old = None
    if target == "fact" and kind:
        row = connection.execute(
            """SELECT value FROM facts WHERE document_id=? AND kind=? AND (? IS NULL OR period_end=?)
               AND (? IS NULL OR period_start=?) ORDER BY id DESC LIMIT 1""",
            (document_id, kind, period_end, period_end, period_start, period_start)).fetchone()
        old = None if row is None else str(row[0])
    if target == "event":
        if event_id is None:
            raise ValueError("en rättelse av en händelse kräver event_id")
        column = "id" if field == "hide" else field
        row = connection.execute(f"SELECT {column} FROM financing_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError(f"händelse {event_id} finns inte")
        old = None if field == "hide" or row[0] is None else str(row[0])
    with connection:
        cursor = connection.execute(
            """INSERT INTO corrections(target, company_id, document_id, event_id, kind, period_start, period_end,
               field, old_value, new_value, reason, author, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (target, company_id, document_id, event_id, kind, period_start, period_end, field, old,
             None if new_value is None else str(new_value), reason.strip(), author, _now()))
    return cursor.lastrowid


def revert_correction(connection: sqlite3.Connection, correction_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM corrections WHERE id=?", (correction_id,)).fetchone()
    if row is None:
        raise ValueError(f"rättelse {correction_id} finns inte")
    with connection:
        connection.execute("UPDATE corrections SET reverted_at=? WHERE id=? AND reverted_at IS NULL",
                           (_now(), correction_id))
    return row


def latest_document(connection: sqlite3.Connection, company_id: int) -> int | None:
    row = connection.execute(
        """SELECT r.document_id FROM reports r JOIN runway_results x ON x.document_id = r.document_id
           WHERE r.company_id=? ORDER BY r.published_at DESC, r.document_id ASC LIMIT 1""", (company_id,)).fetchone()
    return None if row is None else row[0]


# --- Granskningskö och logg ------------------------------------------------------------------------

def queue(connection: sqlite3.Connection, since: str | None = None, limit: int | None = None) -> list[dict]:
    """Bolag att granska, troligaste felen först: flaggor plus bolag med ny rapport eller nyhet sedan `since`."""
    since = since or since_default(connection)
    findings = run_checks(connection)
    changed = {r[0] for r in connection.execute(
        """SELECT company_id FROM reports WHERE substr(published_at,1,10) >= ?
           UNION SELECT company_id FROM news WHERE substr(published_at,1,10) >= ?""", (since, since))}
    by_company: dict[int, list[Finding]] = {}
    for f in findings:
        by_company.setdefault(f.company_id, []).append(f)
    rows = {row["company_id"]: row for row in _latest(connection)}
    out = []
    for company_id in set(by_company) | (changed & set(rows)):
        row = rows[company_id] if company_id in rows else None
        if row is None:
            continue
        result = json.loads(row["result_json"])
        found = by_company.get(company_id, [])
        out.append({
            "company_id": company_id, "name": row["name"], "document_id": row["document_id"],
            "priority": max((f.severity for f in found), default=0), "changed_since": company_id in changed,
            "findings": [{"check": f.check, "detail": f.detail} for f in found],
            "runway": {k: result.get(k) for k in ("status", "period_end", "cash", "monthly_burn", "runway_today",
                                                   "runway_cash_today", "cash_flow_positive", "flags", "periods")},
            "facts": [{"kind": f["kind"], "value": f["value"], "currency": f["currency"],
                       "period": f"{f['period_start'] or ''}–{f['period_end'] or ''}", "page": f["page"],
                       "quote": f["quote"]} for f in _facts(connection, row["document_id"])],
            "events": [{"id": e["id"], "type": e["type"], "status": e["status"], "amount": e["gross_amount"],
                        "currency": e["currency"], "date": e["event_date"], "quote": e["quote"]}
                       for e in connection.execute(
                           "SELECT * FROM financing_events WHERE company_id=? ORDER BY event_date DESC LIMIT 12",
                           (company_id,))],
        })
    out.sort(key=lambda q: (-q["priority"], not q["changed_since"], q["name"].casefold()))
    return out[:limit] if limit else out


def log_run(connection: sqlite3.Connection, *, since: str, checked: int, corrections: int,
            findings_before: int | None, findings_after: int | None, note: str = "") -> int:
    with connection:
        cursor = connection.execute(
            """INSERT INTO review_runs(started_at, finished_at, since, checked, corrections, findings_before,
               findings_after, note) VALUES (?,?,?,?,?,?,?,?)""",
            (_now(), _now(), since, checked, corrections, findings_before, findings_after, note))
    return cursor.lastrowid
