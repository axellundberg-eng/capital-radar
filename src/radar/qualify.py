"""Steg 5: prospektpoäng för bolag med kort runway och utan annonserad emission.

Indata är bolagets senaste runway-resultat, marknadsvärde (market_data) och
emissionshistorik (financing_events). Inga AI-anrop; allt räknas om vid varje
läsning.

Kapitalbehov (brutto, det en emission behöver ta in) = max(0, burn/mån x 12 + skulder som förfaller
inom 12 mån - kassa idag - outnyttjad kredit) / (1 - emissionskostnad), avrundat till 0,1 MSEK.
Emissionskostnaden är en schablon på 12 % (avgifter och garantiersättning). Alla belopp räknas om
till SEK med Riksbankens kurs (fx.py) innan storlek och andel bedöms; saknas kursen kan behovet inte
beräknas. Andel = kapitalbehov / marknadsvärde (båda i SEK).

Poäng 0-100 = summan av vikt x faktor, där pass = 1, warn = 0,5, fail = 0:

    runway         20  pass 3-9 mån; warn under 3 (akut); fail annars
    ingen emission 15  fail om en emission redan är annonserad
    storlek        15  pass 8-80 MSEK; warn 5-8 eller 80-120; fail annars
    andel          25  pass <= 25 %; warn 25-40 % eller saknat marknadsvärde; fail > 40 %
    historik       10  pass om tidigare företrädes- eller riktad emission finns (plus)
    datakvalitet    5  warn vid gammal rapport eller saknat marknadsvärde
    sektor         10  pass för life science (Health Care) och tech (Technology);
                       warn för okänd sektor (t.ex. NGM) och övriga sektorer
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .rounds import Event, dedupe

MONTHS_AHEAD = 12
TARGET_RUNWAY = (3.0, 9.0)
SIZE_RANGE = (8e6, 80e6)
SIZE_SOFT = (5e6, 120e6)
SHARE_PASS, SHARE_WARN = 0.25, 0.40
EQUITY_TYPES = ("rights_issue", "directed_issue")
ISSUE_COST = 0.12   # emissionskostnader och garantiersättning, andel av bruttobeloppet
TERMS = ('subscription_commitments', 'guarantee_amount', 'guarantee_fee_pct', 'guarantors', 'subscription_rate_pct', 'guarantee_utilized', 'issue_price', 'shares_issued', 'shares_before', 'discount_pct', 'exercise_price', 'exercise_start', 'exercise_end', 'issue_costs')
# Summa 100. "Uttalat kapitalbehov" (markers.py) väger 15; "ingen annonserad emission" skiljer dåligt mellan
# prospekt (bolag med annonserad emission ligger i en egen sektion) och väger därför bara 5.
WEIGHTS = {"runway": 20, "no_raise": 5, "size": 15, "share": 20, "history": 10, "quality": 5, "sector": 10,
           "capital_need": 15}
TARGET_SECTORS = ("health", "technology", "life science", "medical", "biotech", "pharma")
FACTOR = {"pass": 1.0, "warn": 0.5, "fail": 0.0, "info": 0.0}
TYPE_TEXT = {"rights_issue": "företrädesemission", "directed_issue": "riktad emission"}


@dataclass
class Check:
    key: str
    label: str
    status: str            # pass, warn, fail, info
    reason: str
    weight: int


@dataclass
class Qualification:
    capital_need: float | None
    cash_today: float | None
    market_cap: float | None
    share: float | None
    score: int
    checks: list[Check] = field(default_factory=list)
    summary: str = ""
    last_issue: dict | None = None
    need_breakdown: dict | None = None   # delarna i kapitalbehovet, i SEK

    def to_dict(self) -> dict:
        return asdict(self)


def msek(value: float) -> str:
    text = f"{value / 1e6:.1f}".replace(".", ",")
    return f"{text.removesuffix(',0')} MSEK"


def cash_today(item: dict) -> float | None:
    """Kassa idag i rapportens valuta: runway-motorns värde, annars rapportens kassa + bekräftad
    finansiering - burn sedan periodslut."""
    if item.get("cash_today") is not None:
        return item["cash_today"]
    if item.get("cash") is None or item.get("monthly_burn") is None:
        return None
    since = item.get("months_since_report") or 0.0
    return max(0.0, item["cash"] + (item.get("confirmed_financing") or 0.0) - item["monthly_burn"] * since)


def _to_sek(item: dict, rates: dict | None) -> float | None:
    currency = (item.get("currency") or "SEK").upper()
    if currency == "SEK":
        return 1.0
    return (rates or {}).get(currency)


def need_breakdown(item: dict, rates: dict | None = None) -> dict | None:
    """Kapitalbehovets delar i SEK, eller None om det inte kan beräknas (positivt kassaflöde, saknad
    kassa eller saknad växelkurs)."""
    cash = cash_today(item)
    factor = _to_sek(item, rates)
    if cash is None or factor is None or item.get("cash_flow_positive") or not item.get("monthly_burn"):
        return None
    operating = item["monthly_burn"] * MONTHS_AHEAD
    debt = item.get("debt_due_12m") or 0.0
    credit = item.get("available_credit") or 0.0
    net = max(0.0, operating + debt - cash - credit)
    gross = net / (1 - ISSUE_COST)
    return {"operating_12m": operating * factor, "debt_due_12m": debt * factor, "cash_today": cash * factor,
            "available_credit": credit * factor, "issue_costs": (gross - net) * factor,
            "net_need": net * factor, "gross_need": round(gross * factor / 1e5) * 1e5,
            "currency": (item.get("currency") or "SEK").upper(), "sek_per_unit": factor}


def capital_need(item: dict, rates: dict | None = None) -> tuple[float | None, float | None]:
    """(kapitalbehov brutto i SEK, kassa idag i SEK); behovet avrundas till 0,1 MSEK."""
    parts = need_breakdown(item, rates)
    if parts is None:
        cash, factor = cash_today(item), _to_sek(item, rates)
        return None, (cash * factor if cash is not None and factor is not None else None)
    return parts["gross_need"], parts["cash_today"]


def equity_events(rows: list[dict]) -> list[Event]:
    """Genomförda eller annonserade aktieemissioner, nyast först."""
    events = [Event(r["type"], r["status"], r.get("gross_amount"), r.get("net_amount"), r.get("currency"),
                    r["event_date"]) for r in rows if r["type"] in EQUITY_TYPES]
    return sorted(dedupe(events), key=lambda e: e.date, reverse=True)


def check_runway(item: dict) -> Check:
    value, (low, high) = item.get("runway_today"), TARGET_RUNWAY
    weight = WEIGHTS["runway"]
    if value is None:
        return Check("runway", "Runway", "fail", "Runway saknas.", weight)
    text = f"Runway idag är {value:.1f} mån".replace(".", ",")
    if low <= value < high:
        return Check("runway", "Runway", "pass", f"{text}, inom målgruppen 3-9 månader.", weight)
    if value < low:
        acute = " (under 1 mån)" if "acute" in (item.get("flags") or []) else ""
        return Check("runway", "Runway", "warn", f"{text}{acute}: akut, kort tid att ordna garanti.", weight)
    return Check("runway", "Runway", "fail", f"{text}, utanför målgruppen 3-9 månader.", weight)


def check_no_raise(item: dict) -> Check:
    weight = WEIGHTS["no_raise"]
    if "raise_announced" in (item.get("flags") or []):
        return Check("no_raise", "Ingen annonserad emission", "fail",
                     "En emission är redan annonserad: garantin är sannolikt ordnad.", weight)
    recent = next((m for m in item.get("markers") or [] if m.get("key") == "recent_raise"), None)
    if recent:
        return Check("no_raise", "Ingen annonserad emission", "fail",
                     f"Tog in kapital {recent.get('date') or 'nyligen'}: nytt kapitalbehov är troligen inte nära.", weight)
    return Check("no_raise", "Ingen annonserad emission", "pass", "Ingen annonserad emission funnen.", weight)


def check_size(need: float | None) -> Check:
    weight, (low, high), (soft_low, soft_high) = WEIGHTS["size"], SIZE_RANGE, SIZE_SOFT
    if need is None:
        return Check("size", "Storlek", "fail", "Kapitalbehov kan inte beräknas.", weight)
    text = f"Kapitalbehovet är {msek(need)}"
    if low <= need <= high:
        return Check("size", "Storlek", "pass", f"{text}, inom 8-80 MSEK.", weight)
    if soft_low <= need <= soft_high:
        return Check("size", "Storlek", "warn", f"{text}, strax utanför 8-80 MSEK.", weight)
    return Check("size", "Storlek", "fail", f"{text}, utanför 8-80 MSEK.", weight)


def check_share(need: float | None, cap: float | None) -> Check:
    weight, label = WEIGHTS["share"], "Andel av marknadsvärde"
    if need is None or not cap:
        return Check("share", label, "warn", "Marknadsvärde okänt: rimligheten går inte att bedöma.", weight)
    share = need / cap
    text = f"Behovet är {share * 100:.0f} % av marknadsvärdet {msek(cap)}"
    if share <= SHARE_PASS:
        return Check("share", label, "pass", f"{text}, högst 25 %.", weight)
    if share <= SHARE_WARN:
        return Check("share", label, "warn", f"{text}: svårt men möjligt att garantera.", weight)
    return Check("share", label, "fail", f"{text}: över 40 %, svår att garantera.", weight)


def check_history(events: list[Event]) -> Check:
    weight = WEIGHTS["history"]
    if not events:
        return Check("history", "Emissionshistorik", "warn",
                     "Ingen tidigare företrädes- eller riktad emission funnen.", weight)
    last = events[0]
    return Check("history", "Emissionshistorik", "pass",
                 f"Tidigare {TYPE_TEXT[last.type]} ({last.date[:10]}); {len(events)} emission(er) i historiken.", weight)


def check_quality(item: dict, cap: float | None) -> Check:
    problems = []
    if "stale_report" in (item.get("flags") or []):
        problems.append("rapporten är gammal")
    if not cap:
        problems.append("marknadsvärde saknas")
    if problems:
        return Check("quality", "Datakvalitet", "warn", "Osäkert underlag: " + " och ".join(problems) + ".",
                     WEIGHTS["quality"])
    return Check("quality", "Datakvalitet", "pass", "Färsk rapport och känt marknadsvärde.", WEIGHTS["quality"])


def check_capital_need(item: dict) -> Check:
    """Uttalat kapitalbehov (markers.py): full poäng för capital, halv för half."""
    weight, label = WEIGHTS["capital_need"], "Uttalat kapitalbehov"
    markers = item.get("markers") or []
    strong = [m["label"] for m in markers if m.get("tier") == "capital"]
    weak = [m["label"] for m in markers if m.get("tier") == "half"]
    if strong:
        return Check("capital_need", label, "pass", ", ".join(strong + weak) + ".", weight)
    if weak:
        return Check("capital_need", label, "warn", ", ".join(weak) + " (indirekt kapitalbehov).", weight)
    return Check("capital_need", label, "fail", "Inget uttalat kapitalbehov funnet.", weight)


def check_sector(sector: str | None, company_sector: dict | None = None) -> Check:
    """Underbransch (sectors.py) i första hand, annars Nasdaqs sektor ur marknadsdatan."""
    sub = company_sector or {}
    if sub.get("group"):
        text = sub.get("label") or sub["group"]
        if sub["group"] in ("life_science", "tech"):
            return Check("sector", "Sektor", "pass", f"{text}: life science eller tech.", WEIGHTS["sector"])
        return Check("sector", "Sektor", "warn", f"{text}: utanför life science och tech.", WEIGHTS["sector"])
    sector = sector or sub.get("icb")
    if not sector:
        return Check("sector", "Sektor", "warn", "Sektor okänd.", WEIGHTS["sector"])
    if any(word in sector.lower() for word in TARGET_SECTORS):
        return Check("sector", "Sektor", "pass", f"{sector}: life science eller tech.", WEIGHTS["sector"])
    return Check("sector", "Sektor", "warn", f"{sector}: utanför life science och tech.", WEIGHTS["sector"])


def score_of(checks: list[Check]) -> int:
    return round(sum(c.weight * FACTOR[c.status] for c in checks))


def summarize(need: float | None, cap: float | None, share: float | None, runway: float | None, score: int) -> str:
    parts = [f"Runway {runway:.1f} mån".replace(".", ",")] if runway is not None else []
    parts.append(f"behöver ca {msek(need)} på 12 månader" if need is not None else "kapitalbehov okänt")
    if share is not None and cap:
        parts.append(f"{share * 100:.0f} % av marknadsvärdet {msek(cap)}")
    else:
        parts.append("marknadsvärde okänt")
    return f"{', '.join(parts)}. Poäng {score}/100."


def qualify(item: dict, market: dict | None, history: list[dict], rates: dict | None = None) -> Qualification:
    """Kvalificera ett bolag. `market` och `history` är rader ur market_data resp. financing_events,
    `rates` växelkurser (SEK per enhet, fx.latest_rates). Alla belopp i resultatet är i SEK."""
    market = market or {}
    cap = market.get("market_cap")
    cap_currency = (market.get("currency") or "SEK").upper()
    if cap is not None and cap_currency != "SEK":
        rate = (rates or {}).get(cap_currency)
        cap = cap * rate if rate else None
    parts = need_breakdown(item, rates)
    need, cash = capital_need(item, rates)
    share = need / cap if need is not None and cap else None
    events = equity_events(history)
    checks = [check_runway(item), check_no_raise(item), check_size(need), check_share(need, cap),
              check_history(events), check_quality(item, cap),
              check_sector(market.get("sector"), item.get("sector")), check_capital_need(item)]
    score = score_of(checks)
    last = events[0] if events else None
    last_issue = ({"type": last.type, "status": last.status, "date": last.date, "amount": last.amount,
                   "currency": last.currency} if last else None)
    return Qualification(need, cash, cap, share, score, checks,
                         summarize(need, cap, share, item.get("runway_today"), score), last_issue, parts)


def load_market(connection: sqlite3.Connection) -> dict[int, dict]:
    return {r["company_id"]: dict(r) for r in connection.execute("SELECT * FROM market_data")}


def load_history(connection: sqlite3.Connection) -> dict[int, list[dict]]:
    """Alla händelser per bolag, med granskningens rättelser (dolda händelser tas bort)."""
    from .financing import reclassify
    from .review import apply_event_row, event_overrides

    overrides = event_overrides(connection)
    history: dict[int, list[dict]] = {}
    for row in connection.execute(
            """SELECT f.*, n.title, n.url FROM financing_events f LEFT JOIN news n ON n.document_id = f.news_document_id
               ORDER BY f.event_date DESC"""):
        fixed = apply_event_row(reclassify(dict(row)), overrides.get(row["id"]))
        if fixed is not None:
            history.setdefault(row["company_id"], []).append(fixed)
    return history


RECENT_DAYS = 730  # 24 månader


def recent_financing(rows: list[dict], today: str) -> tuple[int, list[dict]]:
    """(antal emissioner, affärer) de senaste 24 månaderna, nyast först.

    Varje affär är en runda: beslut, informationsdokument, utfall och registrering (på svenska och
    engelska) slås ihop med samma regel som runway-beräkningen (rounds.dedupe: samma typ inom ett
    tidsfönster och ungefär samma belopp). Raden får bästa status, första och sista datum, antal
    pressmeddelanden och villkoren samlade från alla versioner. Kvittningsemissioner läggs på emissionen
    de hör till. Emissioner (antalet) är företrädes- och riktade emissioner."""
    from .rounds import _merge, _same_round, attach_set_offs

    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=RECENT_DAYS)).isoformat()
    recent = [r for r in rows if r["status"] != "cancelled" and (r["event_date"] or "")[:10] >= cutoff]
    events = [Event(r["type"], r["status"], r["gross_amount"], r["net_amount"], r["currency"], r["event_date"],
                    r.get("quote"), r.get("title"), r.get("url"), r.get("drawn_amount"), r.get("advisers"),
                    **{key: r.get(key) for key in TERMS}) for r in recent]
    groups: list[list[Event]] = []
    for event in sorted(events, key=lambda e: e.date):
        target = next((g for g in groups if any(_same_round(event, other) for other in g)), None)
        (target.append(event) if target is not None else groups.append([event]))
    meta = {}
    merged = []
    for group in groups:
        round_ = _merge(group)
        if round_ is None:
            continue
        merged.append(round_)
        # Länken går till utfallet (senaste pressmeddelandet i affären) när det finns, annars till det första.
        linked = sorted((e for e in group if e.url), key=lambda e: (e.status == "completed", e.date))
        link = (linked[-1].url, linked[-1].title) if linked else (None, None)
        meta[(round_.type, round_.date, round_.amount)] = (min(e.date for e in group)[:10], len(group), link)
    rounds = attach_set_offs(merged)
    equity = [e for e in rounds if e.type in EQUITY_TYPES]
    listing = []
    for e in sorted(rounds, key=lambda e: e.date, reverse=True):
        first, count, (url, title) = meta.get((e.type, e.date, e.amount), (e.date[:10], 1, (None, None)))
        entry = {"type": e.type, "status": e.status, "date": e.date[:10], "first_date": first,
                 "press_releases": count, "amount": e.amount, "currency": e.currency, "quote": e.quote,
                 "url": url, "title": title,
                 "advisers": e.advisers, "paid_in_shares": e.paid_in_shares}
        entry.update({key: getattr(e, key) for key in TERMS})
        before, issued = e.shares_before, e.shares_issued
        # Utspädning = nya aktier / aktier efter emissionen.
        entry["dilution_pct"] = round(100 * issued / (before + issued), 1) if before and issued else None
        listing.append(entry)
    return len(equity), listing


def prospect_companies(connection: sqlite3.Connection, items: list[dict]) -> list[tuple[dict, str | None]]:
    """Bolag i 3-9-, under-3- och akut-grupperna (alltså utan annonserad emission), med ISIN."""
    isins = {r["id"]: r["isin"] for r in connection.execute("SELECT id, isin FROM companies")}
    return [(i, isins.get(i["id"])) for i in items
            if i["band"] in ("3_9", "under_3", "acute") and "raise_announced" not in i["flags"]]
