"""Underlag till fliken Garanti: går en emission realistiskt att garantera? Kontrakt: docs/api-garanti.md.

Samlar runway, kapitalbehov, kursdata, insynshandel, tidigare emissioner, bemyndigande och nästa
rapport för ett bolag. Allt som saknas skickas som None; inget hittas på.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from . import insiders, market
from .authorizations import latest as latest_authorizations
from .calendar import next_report
from .fx import latest_rates
from .qualify import load_history, load_market, need_breakdown, recent_financing

SHARE_EASY, SHARE_POSSIBLE = 15.0, 40.0      # behov i procent av börsvärdet
DAYS_EASY, DAYS_POSSIBLE = 30.0, 120.0       # handelsdagar som behovet motsvarar i omsättning

RUNWAY_FIELDS = ("band", "runway_today", "runway_cash_today", "runway_pro_forma", "cash_today", "monthly_burn",
                 "currency", "period_end", "months_since_report", "burn_basis", "flags")


def assess(share_pct: float | None, days: float | None) -> tuple[str, list[str]]:
    """(bedömning, skäl): lätt vid andel <=15 % och <=30 dagar, möjlig vid <=40 % och <=120 dagar, annars svår.

    Saknas en av siffrorna är bedömningen okänd, utom när den kända redan överskrider möjlig-gränsen:
    då är svår säker oavsett den saknade siffran."""
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
        return "svår", reasons
    if share_pct is None or days is None:
        return "okänd", reasons
    if share_pct <= SHARE_EASY and days <= DAYS_EASY:
        return "lätt", reasons
    return "möjlig", reasons


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
    block["error"] = error
    return block


def capacity_block(need: dict | None, market_info: dict) -> dict:
    gross = (need or {}).get("gross_need")
    cap, turnover = market_info.get("market_cap"), market_info.get("avg_daily_turnover_3m")
    share = round(100 * gross / cap, 1) if gross and cap else None
    days = round(gross / turnover, 1) if gross and turnover else None
    if not gross:
        return {"need_share_of_market_cap_pct": None, "turnover_days_for_need": None, "assessment": "okänd",
                "reasons": ["Kapitalbehovet kan inte beräknas (positivt kassaflöde eller uppgifter saknas)"]}
    assessment, reasons = assess(share, days)
    return {"need_share_of_market_cap_pct": share, "turnover_days_for_need": days, "assessment": assessment,
            "reasons": reasons}


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
    return {
        "company": {"id": row["id"], "name": row["name"], "symbol": row["symbol"], "isin": row["isin"],
                    "market": row["market"], "segment": row["segment"], "sector": (cap_row or {}).get("sector")},
        "runway": runway, "need": need, "market": market_info,
        "capacity": capacity_block(need, market_info),
        "insiders": insiders.recent(connection, company_id, 50),
        "insider_summary": insiders.summary(connection, company_id, today, rates),
        "emissions_24m": emissions, "financing": financing,
        "authorization": latest_authorizations(connection).get(company_id),
        "next_report": next_report(connection, today).get(company_id),
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
