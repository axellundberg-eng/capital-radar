"""Utförsäljning av garantiaktier vid högst 20 % av dagsvolymen.

Garanten säljer alltid direkt: ersättningsaktier och aktier som tilldelas när garantin tas i
anspråk säljs från första handelsdagen efter registreringen, men aldrig mer än SELL_SHARE av
dagens volym. Det farliga är att fastna i en illikvid aktie, så måttet är hur många handelsdagar
utförsäljningen tar och vad den faktiskt gav, inte hur kursen gick långt senare.

Två delar:
- capacity: framåtblickande. Hur stor garanti kan säljas ut på 10 och 20 handelsdagar om den tas
  i anspråk helt, räknat på medianomsättningen (spikar på emissionsdagar räknas inte upp).
- deals: bakåtblickande. För bolagets tidigare garantier: vad hade en garanti på AMOUNT kronor
  gett om aktierna sålts med 20 %-regeln från registreringen? Saknas underlag blir fältet None.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

SELL_SHARE = 0.20            # högst så stor andel av dagens volym säljs
REGISTRATION_DAYS = 10       # handelsdagar från utfall till att aktierna kan säljas (registrering)
MAX_EXIT_DAYS = 60           # längre än så räknas som att fastna
MEDIAN_DAYS = 60             # handelsdagar som medianomsättningen räknas på
MIN_MEDIAN_DAYS = 20
DEFAULT_AMOUNT = 1_000_000.0
DEAL_GAP_DAYS = 45           # garantiposter närmare än så hör till samma affär
OUTCOME_WINDOW_DAYS = 75     # utfall och kvittning letas så här långt efter affärens första post
OUTCOME_TYPES = ("rights_issue", "directed_issue", "warrants")


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def median_turnover(points: list[dict], factor: float = 1.0) -> float | None:
    """Medianomsättning (kurs x volym, i kronor) de senaste MEDIAN_DAYS handelsdagarna."""
    traded = [p for p in points if p.get("volume") is not None][-MEDIAN_DAYS:]
    if len(traded) < MIN_MEDIAN_DAYS:
        return None
    return median(p["close"] * p["volume"] for p in traded) * factor


def capacity(turnover_per_day: float | None, amount: float = DEFAULT_AMOUNT, basis: str | None = None) -> dict:
    """Hur stor garanti som går att sälja ut om den tas i anspråk helt, vid SELL_SHARE av omsättningen."""
    block = {"sell_share_pct": round(SELL_SHARE * 100), "amount": amount, "turnover_per_day": turnover_per_day,
             "turnover_basis": basis, "sell_per_day": None, "days_full_utilization": None,
             "max_guarantee_10d": None, "max_guarantee_20d": None}
    if not turnover_per_day:
        return block
    per_day = SELL_SHARE * turnover_per_day
    block.update(sell_per_day=round(per_day), days_full_utilization=round(amount / per_day, 1),
                 max_guarantee_10d=round(10 * per_day), max_guarantee_20d=round(20 * per_day))
    return block


def simulate(points: list[dict], after: str, shares: float, *, skip: int = REGISTRATION_DAYS,
             share: float = SELL_SHARE, max_days: int = MAX_EXIT_DAYS) -> dict | None:
    """Sälj `shares` aktier med start `skip` handelsdagar efter datumet `after`, högst `share` av dagsvolymen.

    None om kursdata saknas för perioden. `complete` är falskt om aktierna inte hann säljas inom
    max_days handelsdagar (fastnat) eller om kursdatan tar slut innan (`open`: utförsäljningen pågår)."""
    later = [p for p in points if p["date"] > after[:10]]
    if len(later) <= skip:
        return None
    window = later[skip:skip + max_days]
    remaining, proceeds, used, last = shares, 0.0, 0, None
    for point in window:
        if remaining <= 1e-9:
            break
        used += 1
        last = point
        sold = min(remaining, share * (point.get("volume") or 0.0))
        remaining -= sold
        proceeds += sold * point["close"]
    sold_total = shares - remaining
    complete = remaining <= 1e-9
    return {"start": window[0]["date"], "end": (last or window[0])["date"], "days": used,
            "shares": shares, "sold": sold_total, "proceeds": proceeds,
            "avg_price": proceeds / sold_total if sold_total else None,
            "remaining": max(remaining, 0.0), "remaining_value": max(remaining, 0.0) * (last or window[0])["close"],
            "complete": complete, "open": not complete and len(window) < max_days}


def _first(rows: list[dict], key: str):
    return next((r[key] for r in rows if r.get(key) is not None), None)


def _max(rows: list[dict], key: str):
    values = [r[key] for r in rows if r.get(key) is not None]
    return max(values) if values else None


def _groups(rows: list[dict]) -> list[list[dict]]:
    """Garantiposter (belopp eller ersättning angiven) grupperade per affär, äldst först."""
    posts = sorted((r for r in rows if r.get("status") != "cancelled" and _day(r.get("event_date"))
                    and (r.get("guarantee_amount") or r.get("guarantee_fee_pct"))),
                   key=lambda r: r["event_date"])
    groups: list[list[dict]] = []
    for row in posts:
        if groups and _day(row["event_date"]) - _day(groups[-1][0]["event_date"]) <= timedelta(days=DEAL_GAP_DAYS):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def deal_terms(group: list[dict], rows: list[dict]) -> dict:
    """Villkoren för en garantiaffär: belopp, ersättning (kontant eller i aktier), kurs, utfall, ianspråktagande."""
    start = _day(group[0]["event_date"])
    end = start + timedelta(days=OUTCOME_WINDOW_DAYS)
    window = [r for r in rows if r.get("status") != "cancelled" and (d := _day(r.get("event_date")))
              and start <= d <= end]
    main = [r for r in group if r["type"] != "set_off"]
    set_offs = [r for r in window if r["type"] == "set_off"]
    outcomes = [r for r in window if r["type"] in OUTCOME_TYPES and r.get("status") == "completed"
                and (r.get("subscription_rate_pct") is not None or r.get("guarantee_utilized") is not None)]
    outcome = max(outcomes, key=lambda r: r["event_date"]) if outcomes else None
    guarantee = _max(group, "guarantee_amount")
    issue_price = _first(main, "issue_price") or _first(window, "issue_price")
    cash_fee = _max(main, "guarantee_fee_pct")
    share_fee = _max(set_offs, "guarantee_fee_pct")
    fee_in_shares = bool(set_offs) and (share_fee is not None or cash_fee is not None)
    fee_price = _first(set_offs, "issue_price") or issue_price
    subscription = (outcome or {}).get("subscription_rate_pct")
    utilized = _first(([outcome] if outcome else []) + group, "guarantee_utilized")
    offered = _max([r for r in group + ([outcome] if outcome else []) if r["type"] != "set_off"], "gross_amount")
    share_used, estimated = None, False
    if utilized is not None and guarantee:
        share_used = min(1.0, utilized / guarantee)
    elif subscription is not None and subscription >= 100:
        share_used = 0.0
    elif subscription is not None and guarantee and offered:
        share_used = min(1.0, offered * (1 - subscription / 100) / guarantee)
        estimated = True
    return {"date": group[0]["event_date"][:10], "type": group[0]["type"], "guarantee_amount": guarantee,
            "currency": group[0].get("currency") or "SEK", "guarantors": _first(group, "guarantors"),
            "issue_price": issue_price, "fee_pct": share_fee if fee_in_shares and share_fee is not None else cash_fee,
            "cash_fee_pct": cash_fee, "fee_in_shares": fee_in_shares, "fee_price": fee_price,
            "outcome_date": (outcome or {}).get("event_date", "")[:10] or None,
            "subscription_rate_pct": subscription, "utilized_share": share_used, "utilized_estimated": estimated}


def deal_return(terms: dict, points: list[dict], amount: float = DEFAULT_AMOUNT) -> dict:
    """Vad en garanti på `amount` hade gett med försäljning vid 20 % av dagsvolymen. Fälten är None när underlag saknas."""
    result = {**terms, "amount": amount, "result": None, "result_pct": None, "exit": None, "missing": None}
    fee, used, price = terms["fee_pct"], terms["utilized_share"], terms["issue_price"]
    if fee is None:
        result["missing"] = "ersättningen saknas"
        return result
    if used is None:
        result["missing"] = "utfallet saknas"
        return result
    fee_shares = amount * fee / 100 / terms["fee_price"] if terms["fee_in_shares"] and terms["fee_price"] else 0.0
    if terms["fee_in_shares"] and not terms["fee_price"]:
        result["missing"] = "teckningskursen saknas"
        return result
    if used and not price:
        result["missing"] = "teckningskursen saknas"
        return result
    allotted = amount * used / price if used else 0.0
    cash = 0.0 if terms["fee_in_shares"] else amount * fee / 100
    cost = amount * used
    shares = fee_shares + allotted
    if shares <= 0:
        result.update(result=round(cash), result_pct=round(100 * cash / amount, 1))
        return result
    sim = simulate(points, terms["outcome_date"] or terms["date"], shares)
    if sim is None:
        result["missing"] = "kursdata saknas för perioden"
        return result
    value = sim["proceeds"] + sim["remaining_value"]
    total = value + cash - cost
    result.update(exit=sim, result=round(total), result_pct=round(100 * total / amount, 1))
    return result


def block(points: list[dict], rows: list[dict], avg_turnover: float | None, *, factor: float = 1.0,
          amount: float = DEFAULT_AMOUNT) -> dict:
    """Hela likviditetsdelen i /api/guarantee: kapacitet framåt och utfall i tidigare garantier."""
    med = median_turnover(points, factor)
    turnover, basis = (med, "median_60d") if med else (avg_turnover, "mean_3m" if avg_turnover else None)
    deals = [deal_return(deal_terms(g, rows), points, amount) for g in _groups(rows)]
    return {"capacity": capacity(turnover, amount, basis), "deals": list(reversed(deals)),
            "registration_days": REGISTRATION_DAYS, "max_exit_days": MAX_EXIT_DAYS}
