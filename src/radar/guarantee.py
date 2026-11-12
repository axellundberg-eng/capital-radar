"""Underlag till fliken Garanti: går en emission realistiskt att garantera? Kontrakt: docs/api-garanti.md.

Samlar runway, kapitalbehov, kursdata, insynshandel, tidigare emissioner, bemyndigande och nästa
rapport för ett bolag. Allt som saknas skickas som None; inget hittas på.
"""

from __future__ import annotations

import re
import json
import sqlite3
from datetime import date, datetime, timezone

from . import insiders, market, owners, shorts
from .authorizations import latest as latest_authorizations
from .calendar import next_report
from .fx import latest_rates
from .qualify import load_history, load_market, need_breakdown, recent_financing

EQUITY_ISSUE_TYPES = ("rights_issue", "directed_issue")
SHARE_EASY, SHARE_POSSIBLE = 15.0, 40.0      # behov i procent av börsvärdet
DAYS_EASY, DAYS_POSSIBLE = 30.0, 120.0       # handelsdagar som behovet motsvarar i omsättning

CONCENTRATION_HIGH = 50.0        # topp 3-ägarnas andel av kapitalet (%) som gör garantin lättare
SHORT_HIGH = 2.0                 # total blankning (%) som gör den svårare
SUBSCRIPTION_LOW = 60.0          # historisk teckningsgrad (%) som gör den svårare
GUARANTEE_FIELDS = ("guarantee_amount", "guarantee_utilized", "guarantee_fee_pct", "guarantors",
                    "subscription_rate_pct", "subscription_commitments")

RUNWAY_FIELDS = ("band", "runway_today", "runway_cash_today", "runway_pro_forma", "cash_today", "monthly_burn",
                 "currency", "period_end", "months_since_report", "burn_basis", "flags")


def assess(share_pct: float | None, days: float | None, *, top3_pct: float | None = None,
           short_total_pct: float | None = None, avg_subscription_rate_pct: float | None = None
           ) -> tuple[str, list[str]]:
    """(bedömning, skäl): lätt vid andel <=15 % och <=30 dagar, möjlig vid <=40 % och <=120 dagar, annars svår.

    Saknas en av siffrorna är bedömningen okänd, utom när den kända redan överskrider möjlig-gränsen:
    då är svår säker oavsett den saknade siffran.

    Tilläggen (när underlaget finns) ger egna skäl: hög ägarkoncentration (topp 3 > 50 %) gör garantin lättare
    (en möjlig bedömning blir lätt om inget talar emot); hög blankning (> 2 %) och låg historisk teckningsgrad
    (< 60 %) gör den svårare (ett steg ned: lätt blir möjlig, möjlig blir svår). Svår och okänd ändras inte."""
    reasons: list[str] = []
    if share_pct is not None:
        reasons.append(f"Behovet motsvarar {share_pct:.0f} % av börsvärdet")
    else:
        reasons.append("Börsvärde eller kapitalbehov saknas")
    if days is not None:
        reasons.append(f"Behovet motsvarar {days:.0f} handelsdagars omsättning")
    else:
        reasons.append("Omsättning i aktien saknas")
    if (share_pct is not None and share_pct > SHARE_POSSIBLE) or (days is not None and days > DAYS_POSSIBLE):
        verdict = "svår"
    elif share_pct is None or days is None:
        verdict = "okänd"
    elif share_pct <= SHARE_EASY and days <= DAYS_EASY:
        verdict = "lätt"
    else:
        verdict = "möjlig"
    helps = top3_pct is not None and top3_pct > CONCENTRATION_HIGH
    headwinds = 0
    if helps:
        reasons.append(f"Hög ägarkoncentration: de tre största ägarna har {top3_pct:.0f} % av kapitalet "
                       "och kan teckna en större del")
    if short_total_pct is not None and short_total_pct > SHORT_HIGH:
        headwinds += 1
        reasons.append(f"Hög blankning: {short_total_pct:.1f} % av aktierna är blankade")
    if avg_subscription_rate_pct is not None and avg_subscription_rate_pct < SUBSCRIPTION_LOW:
        headwinds += 1
        reasons.append(f"Låg historisk teckningsgrad: i snitt {avg_subscription_rate_pct:.0f} % i tidigare emissioner")
    if headwinds and verdict in ("lätt", "möjlig"):
        verdict = "möjlig" if verdict == "lätt" else "svår"
    elif helps and verdict == "möjlig":
        verdict = "lätt"
    return verdict, reasons


def _sek(value: float | None, currency: str | None, rates: dict[str, float]) -> float | None:
    if value is None:
        return None
    factor = 1.0 if (currency or "SEK").upper() == "SEK" else rates.get((currency or "").upper())
    return value * factor if factor else None


def market_block(cap_row: dict | None, history: dict | None, rates: dict[str, float]) -> dict:
    history = history or {}
    cap = _sek((cap_row or {}).get("market_cap"), (cap_row or {}).get("currency"), rates)
    error = history.get("error") if history else None
    if error is None and cap_row and cap_row.get("error") and cap is None:
        error = cap_row["error"]
    block = {"market_cap": cap}
    for field in ("price", "price_change_1m_pct", "price_change_3m_pct", "price_change_12m_pct",
                  "avg_daily_turnover_3m", "avg_daily_volume_3m", "high_52w", "low_52w", "as_of", "source"):
        block[field] = history.get(field)
    try:   # veckokurser för kursdiagrammet: [[datum, kurs], ...]
        block["series"] = json.loads(history.get("series") or "[]")
    except ValueError:
        block["series"] = []
    block["error"] = error
    return block


def capacity_block(need: dict | None, market_info: dict, extras: dict | None = None) -> dict:
    gross = (need or {}).get("gross_need")
    cap, turnover = market_info.get("market_cap"), market_info.get("avg_daily_turnover_3m")
    share = round(100 * gross / cap, 1) if gross and cap else None
    days = round(gross / turnover, 1) if gross and turnover else None
    if not gross:
        return {"need_share_of_market_cap_pct": None, "turnover_days_for_need": None, "assessment": "okänd",
                "reasons": ["Kapitalbehovet kan inte beräknas (positivt kassaflöde eller uppgifter saknas)"]}
    assessment, reasons = assess(share, days, **(extras or {}))
    return {"need_share_of_market_cap_pct": share, "turnover_days_for_need": days, "assessment": assessment,
            "reasons": reasons}


def _split_guarantors(text: str | None) -> list[str]:
    names, seen = [], set()
    for part in re.split(r"[;,]", text or ""):
        name = re.sub(r"\s+", " ", part).strip()
        key = insiders.normalize_name(name)
        if key and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _mean(values: list[float], digits: int = 1) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _amount_key(amount: float | None) -> int | None:
    return round(amount / 1e5) if amount else None


def guarantee_history(rows: list[dict]) -> dict | None:
    """Emissioner med garanti- eller teckningsuppgifter (nyast först) och garanterna sammanvägda; None om inga."""
    issues: list[dict] = []
    for r in sorted(rows, key=lambda r: r.get("event_date") or "", reverse=True):
        if r["type"] not in EQUITY_ISSUE_TYPES or r["status"] == "cancelled":
            continue
        if all(r.get(f) is None for f in GUARANTEE_FIELDS):
            continue
        amount = r["gross_amount"] if r.get("gross_amount") is not None else r.get("net_amount")
        date_text = (r.get("event_date") or "")[:10]
        # Svensk och engelsk version av samma pressmeddelande ger en post; luckor fylls ur den andra.
        twin = next((i for i in issues if i["type"] == r["type"] and i["date"] == date_text
                     and _amount_key(i["amount"]) == _amount_key(amount)), None)
        if twin:
            for key in ("subscription_rate_pct", "guarantee_amount", "guarantee_utilized", "guarantee_fee_pct",
                        "guarantors"):
                if twin[key] is None and r.get(key) is not None:
                    twin[key] = r[key]
            continue
        issues.append({"date": date_text, "type": r["type"], "amount": amount, "currency": r.get("currency") or "SEK",
                       "subscription_rate_pct": r.get("subscription_rate_pct"),
                       "guarantee_amount": r.get("guarantee_amount"), "guarantee_utilized": r.get("guarantee_utilized"),
                       "guarantee_fee_pct": r.get("guarantee_fee_pct"), "guarantors": r.get("guarantors")})
    if not issues:
        return None
    stats: dict[str, dict] = {}
    for issue in issues:
        for name in _split_guarantors(issue["guarantors"]):
            entry = stats.setdefault(insiders.normalize_name(name), {"name": name, "issues": 0, "fees": []})
            entry["issues"] += 1
            if issue["guarantee_fee_pct"] is not None:
                entry["fees"].append(issue["guarantee_fee_pct"])
    guarantors = sorted(({"name": e["name"], "issues": e["issues"], "avg_fee_pct": _mean(e["fees"], 2)}
                         for e in stats.values()), key=lambda g: (-g["issues"], g["name"].casefold()))
    return {"issues": issues, "guarantors": guarantors,
            "avg_subscription_rate_pct": _mean([i["subscription_rate_pct"] for i in issues
                                                if i["subscription_rate_pct"] is not None])}


def warrants_outstanding(rows: list[dict], today: str) -> list[dict]:
    """Teckningsoptioner vars lösenperiod inte löpt ut (eller saknar slutdatum och är annonserade), nyast först."""
    result, seen = [], set()
    for r in sorted(rows, key=lambda r: r.get("event_date") or "", reverse=True):
        if r["type"] != "warrants" or r["status"] == "cancelled":
            continue
        end = (r.get("exercise_end") or "")[:10] or None
        if not (end >= today[:10] if end else r["status"] == "announced"):
            continue
        key = ((r.get("event_date") or "")[:10], r.get("exercise_price"), end)
        if key in seen:
            continue
        seen.add(key)
        amount = r["gross_amount"] if r.get("gross_amount") is not None else r.get("net_amount")
        result.append({"date": key[0], "exercise_price": r.get("exercise_price"),
                       "exercise_start": r.get("exercise_start"), "exercise_end": end,
                       "amount": amount, "currency": r.get("currency")})
    return result


def build(connection: sqlite3.Connection, company_id: int, today: str | None = None) -> dict | None:
    """Hela /api/guarantee-svaret, eller None om bolaget saknas."""
    from .web import FLAG_TEXT, latest_rows

    today = today or date.today().isoformat()
    row = connection.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if row is None:
        return None
    rates = latest_rates(connection)
    cap_row = load_market(connection).get(company_id)
    item = next((i for i in latest_rows(connection) if i["id"] == company_id), None)
    history = load_history(connection).get(company_id, [])
    emissions, financing = recent_financing(history, today)
    need = need_breakdown(item, rates) if item else None
    market_info = market_block(cap_row, market.load(connection, company_id), rates)
    runway = {field: (item or {}).get(field) for field in RUNWAY_FIELDS}
    runway["band"] = (item or {}).get("band", "missing")
    runway["flags"] = list(runway["flags"] or [])
    runway["flag_text"] = {flag: FLAG_TEXT.get(flag, flag) for flag in runway["flags"]}
    owner_info = owners.latest(connection, company_id)
    short_list = shorts.for_company(connection, company_id)
    short_total = round(sum(p["pct"] for p in short_list), 2) if short_list else None
    guarantee_info = guarantee_history(history)
    extras = {"top3_pct": (owner_info or {}).get("top3_pct"), "short_total_pct": short_total,
              "avg_subscription_rate_pct": (guarantee_info or {}).get("avg_subscription_rate_pct")}
    return {
        "company": {"id": row["id"], "name": row["name"], "symbol": row["symbol"], "isin": row["isin"],
                    "market": row["market"], "segment": row["segment"], "sector": (cap_row or {}).get("sector")},
        "runway": runway, "need": need, "market": market_info,
        "capacity": capacity_block(need, market_info, extras),
        "insiders": insiders.recent(connection, company_id, 50),
        "insider_summary": insiders.summary(connection, company_id, today, rates),
        "emissions_24m": emissions, "financing": financing,
        "authorization": latest_authorizations(connection).get(company_id),
        "next_report": next_report(connection, today).get(company_id),
        "owners": owner_info, "short_positions": short_list, "short_total_pct": short_total,
        "guarantee_history": guarantee_info, "warrants_outstanding": warrants_outstanding(history, today),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def search(connection: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """Bolag vars namn, ticker eller ISIN innehåller texten (skiftlägesokänsligt); namn som börjar med den först."""
    from .web import latest_rows

    needle = (query or "").strip().casefold()
    if not needle:
        return []
    rows = connection.execute("SELECT id, name, symbol, isin, market FROM companies").fetchall()
    hits = [r for r in rows if any(needle in (r[k] or "").casefold() for k in ("name", "symbol", "isin"))]
    hits.sort(key=lambda r: (not (r["name"] or "").casefold().startswith(needle),
                             (r["symbol"] or "").casefold() != needle, (r["name"] or "").casefold()))
    hits = hits[:max(0, limit)]
    by_id = {i["id"]: i for i in latest_rows(connection)} if hits else {}
    return [{"id": r["id"], "name": r["name"], "symbol": r["symbol"], "isin": r["isin"], "market": r["market"],
             "band": by_id.get(r["id"], {}).get("band", "missing"),
             "runway_today": by_id.get(r["id"], {}).get("runway_today")} for r in hits]
