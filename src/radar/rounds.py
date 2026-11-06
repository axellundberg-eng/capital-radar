"""Finansieringshändelser efter rapporten blir finansieringsrundor.

Samma kapitalanskaffning syns ofta flera gånger (beslut, utfall). Händelser av
samma typ inom 120 dagar och med liknande belopp räknas som en runda; den mest
avancerade statusen vinner (genomförd > annonserad) och en avbruten runda
tas bort. Bara genomförda rundor efter rapportens periodslut ger kassa.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields, replace
from datetime import date

WINDOW_DAYS = 120
AMOUNT_TOLERANCE = 0.25
EQUITY_COST = 0.9           # ca 10 % emissionskostnader när nettobelopp saknas
DEBT_TYPES = ("loan", "bridge_loan", "convertible")
OUTFLOW_TYPES = ("repayment",)  # amortering, återbetalning, kontant inlösen: pengar ut
# Annonserade lån, kreditramar och återbetalningar är ingen publik kapitalanskaffning
# (emission, brygglån inför emission, konvertibel och teckningsoptioner är det).
# Kvittningsemission (t.ex. garantiersättning i aktier): aktier mot en fordran, inga pengar in.
NON_CASH_TYPES = ("set_off",)
NO_RAISE_TYPES = OUTFLOW_TYPES + NON_CASH_TYPES + ("credit_facility", "loan")
SET_OFF = re.compile(r"kvittning|kvittat|set[- ]off|ersättningsemission|compensation issue|"
                     r"betalning (?:genom|via) kvittning|payment by way of set", re.I)
PARENT_DAYS = 45   # en kvittningsemission hör till emissionen den ersätter garanterna för


def is_set_off(*texts: str | None) -> bool:
    """Är händelsen en kvittningsemission (ingen kontant betalning)?"""
    return any(text and SET_OFF.search(text) for text in texts)
RANK = {"announced": 1, "completed": 2}


@dataclass(frozen=True)
class Event:
    type: str
    status: str
    gross_amount: float | None
    net_amount: float | None
    currency: str | None
    date: str
    quote: str | None = None
    title: str | None = None
    url: str | None = None
    drawn_amount: float | None = None   # utnyttjad del av en kreditfacilitet
    advisers: str | None = None         # finansiella rådgivare, emissionsinstitut och garanter, som de nämns
    # Villkor och utfall (financing.py). Belopp i händelsens valuta, priser per aktie, procent 0-100.
    subscription_commitments: float | None = None   # teckningsåtaganden
    guarantee_amount: float | None = None           # garanterat belopp
    guarantee_fee_pct: float | None = None          # garantiersättning i procent av garanterat belopp
    guarantors: str | None = None
    subscription_rate_pct: float | None = None      # teckningsgrad i utfallet
    guarantee_utilized: float | None = None         # utnyttjad garanti
    issue_price: float | None = None                # teckningskurs per aktie
    shares_issued: float | None = None
    shares_before: float | None = None
    discount_pct: float | None = None               # rabatt mot aktiekursen enligt pressmeddelandet
    exercise_price: float | None = None             # teckningsoptioner: lösenpris
    exercise_start: str | None = None               # teckningsoptioner: lösenperiodens början
    exercise_end: str | None = None
    issue_costs: float | None = None                # emissionskostnader enligt pressmeddelandet
    paid_in_shares: float | None = None             # kvittningsemission till garanter som hör till rundan
    original_currency: str | None = None            # satt när beloppen räknats om till rapportens valuta
    original_amount: float | None = None

    @property
    def amount(self) -> float | None:
        return self.gross_amount if self.gross_amount is not None else self.net_amount


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)


def _same_round(a: Event, b: Event) -> bool:
    if a.type != b.type or _days(a.date, b.date) > WINDOW_DAYS:
        return False
    if a.amount is None or b.amount is None:
        return True
    return abs(a.amount - b.amount) <= AMOUNT_TOLERANCE * max(a.amount, b.amount)


def _merge(group: list[Event]) -> Event | None:
    """En händelse per runda: bästa status, luckor i belopp fylls från de andra."""
    if any(e.status == "cancelled" for e in group) and not any(e.status == "completed" for e in group):
        return None
    best = max(group, key=lambda e: (RANK.get(e.status, 0), e.net_amount is not None, e.date))
    gross = best.gross_amount if best.gross_amount is not None else next(
        (e.gross_amount for e in group if e.gross_amount is not None), None)
    net = best.net_amount if best.net_amount is not None else next(
        (e.net_amount for e in group if e.net_amount is not None), None)
    drawn = best.drawn_amount if best.drawn_amount is not None else next(
        (e.drawn_amount for e in group if e.drawn_amount is not None), None)
    status = "completed" if best.status == "completed" else "announced"
    # Villkor som bara står i en av versionerna (t.ex. utfallet eller den engelska) följer med.
    extras = {f: next((getattr(e, f) for e in [best, *group] if getattr(e, f) is not None), None)
              for f in EXTRA_FIELDS}
    return replace(best, status=status, gross_amount=gross, net_amount=net, drawn_amount=drawn, **extras)


EXTRA_FIELDS = tuple(f.name for f in fields(Event)
                     if f.name not in ("type", "status", "gross_amount", "net_amount", "currency", "date",
                                       "quote", "title", "url", "drawn_amount"))
AMOUNT_FIELDS = ("gross_amount", "net_amount", "drawn_amount", "subscription_commitments", "guarantee_amount",
                 "guarantee_utilized")


def to_currency(event: Event, currency: str, fx) -> Event | None:
    """Händelsen med beloppen omräknade till `currency`, eller None om en kurs saknas."""
    if fx is None:
        return None
    converted = {}
    for name in AMOUNT_FIELDS:
        value = getattr(event, name)
        if value is None:
            continue
        new = fx(value, event.currency, currency, event.date)
        if new is None:
            return None
        converted[name] = new
    return replace(event, currency=currency, original_currency=event.currency,
                   original_amount=event.amount, **converted)


def attach_set_offs(rounds: list[Event]) -> list[Event]:
    """Lägg kvittningsemissioner på emissionen de hör till (närmast i tid, inom PARENT_DAYS dagar),
    så att de visas som en del av samma affär. Kvittningar utan emission i närheten står kvar."""
    equity = [i for i, e in enumerate(rounds) if e.type in ("rights_issue", "directed_issue")]
    out = list(rounds)
    drop = set()
    for i, event in enumerate(rounds):
        if event.type not in NON_CASH_TYPES:
            continue
        near = [j for j in equity if _days(rounds[j].date, event.date) <= PARENT_DAYS]
        if not near:
            continue
        j = min(near, key=lambda k: _days(rounds[k].date, event.date))
        out[j] = replace(out[j], paid_in_shares=(out[j].paid_in_shares or 0.0) + (event.amount or 0.0))
        drop.add(i)
    return [e for i, e in enumerate(out) if i not in drop]


def dedupe(events: list[Event]) -> list[Event]:
    groups: list[list[Event]] = []
    for event in sorted(events, key=lambda e: e.date):
        target = next((g for g in groups if any(_same_round(event, other) for other in g)), None)
        if target is None:
            groups.append([event])
        else:
            target.append(event)
    return [merged for merged in (_merge(g) for g in groups) if merged is not None]


def confirmed_cash(event: Event) -> float:
    """Kassaeffekt av en genomförd runda: netto, annars brutto minus ca 10 % för aktier.
    Återbetalningar ger ett negativt belopp. En kreditfacilitet ger bara det utnyttjade beloppet."""
    if event.type == "credit_facility":
        return event.drawn_amount or 0.0
    if event.type in OUTFLOW_TYPES:
        return -(event.amount or 0.0)
    if event.type in NON_CASH_TYPES:
        return 0.0          # kvittning: aktier mot en fordran, ingen kassa
    if event.net_amount is not None:
        return event.net_amount
    if event.gross_amount is None:
        return 0.0
    if event.issue_costs is not None:
        return max(0.0, event.gross_amount - event.issue_costs)   # angivna kostnader går före schablonen
    return event.gross_amount if event.type in DEBT_TYPES else event.gross_amount * EQUITY_COST


def undrawn_credit(event: Event) -> float:
    """Outnyttjad del av en genomförd (beviljad) kreditfacilitet; annars 0."""
    if event.type != "credit_facility" or event.status != "completed" or event.amount is None:
        return 0.0
    return max(0.0, event.amount - (event.drawn_amount or 0.0))


def summarize(events: list[Event], period_end: str, currency: str | None = None, fx=None
              ) -> tuple[float, float, list[dict], float]:
    """(bekräftad kassa, annonserat belopp, rundor som listor, tillgänglig kredit) efter periodslut.

    Kreditfaciliteter räknas inte som kassa (bara utnyttjat belopp) utan som kreditreserv.
    Händelser i annan valuta räknas om med `fx(belopp, från, till, datum)` före sammanslagningen; går
    det inte (kurs saknas) räknas de inte och markeras `unconverted` i listan."""
    after, foreign = [], []
    for event in events:
        if event.date[:10] <= period_end[:10]:
            continue
        if currency and event.currency and event.currency != currency:
            converted = to_currency(event, currency, fx)
            if converted is None:
                foreign.append(event)
                continue
            event = converted
        after.append(event)
    rounds = attach_set_offs(dedupe(after))
    confirmed = announced = credit = 0.0
    for event in rounds:
        if event.status == "completed":
            confirmed += confirmed_cash(event)
            credit += undrawn_credit(event)
        elif event.amount is not None and event.type not in NO_RAISE_TYPES:
            announced += event.amount
    listing = [{**asdict(e), "amount": e.amount, "cash": confirmed_cash(e) if e.status == "completed" else 0.0}
               for e in sorted(rounds, key=lambda e: e.date, reverse=True)]
    listing += [{**asdict(e), "amount": e.amount, "cash": 0.0, "unconverted": True}
                for e in dedupe(foreign) if e.status != "cancelled"]
    return confirmed, announced, listing, credit
