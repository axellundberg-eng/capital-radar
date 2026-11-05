"""Finansieringshändelser efter rapporten blir finansieringsrundor.

Samma kapitalanskaffning syns ofta flera gånger (beslut, utfall). Händelser av
samma typ inom 120 dagar och med liknande belopp räknas som en runda; den mest
avancerade statusen vinner (genomförd > annonserad) och en avbruten runda
tas bort. Bara genomförda rundor efter rapportens periodslut ger kassa.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

WINDOW_DAYS = 120
AMOUNT_TOLERANCE = 0.25
EQUITY_COST = 0.9           # ca 10 % emissionskostnader när nettobelopp saknas
DEBT_TYPES = ("loan", "bridge_loan", "convertible")
OUTFLOW_TYPES = ("repayment",)  # amortering, återbetalning, kontant inlösen: pengar ut
# Annonserade lån, kreditramar och återbetalningar är ingen publik kapitalanskaffning
# (emission, brygglån inför emission, konvertibel och teckningsoptioner är det).
NO_RAISE_TYPES = OUTFLOW_TYPES + ("credit_facility", "loan")
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
    return Event(best.type, status, gross, net, best.currency, best.date, best.quote, best.title, best.url, drawn)


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
    if event.net_amount is not None:
        return event.net_amount
    if event.gross_amount is None:
        return 0.0
    return event.gross_amount if event.type in DEBT_TYPES else event.gross_amount * EQUITY_COST


def undrawn_credit(event: Event) -> float:
    """Outnyttjad del av en genomförd (beviljad) kreditfacilitet; annars 0."""
    if event.type != "credit_facility" or event.status != "completed" or event.amount is None:
        return 0.0
    return max(0.0, event.amount - (event.drawn_amount or 0.0))


def summarize(events: list[Event], period_end: str, currency: str | None = None
              ) -> tuple[float, float, list[dict], float]:
    """(bekräftad kassa, annonserat belopp, rundor som listor, tillgänglig kredit) efter periodslut.

    Kreditfaciliteter räknas inte som kassa (bara utnyttjat belopp) utan som kreditreserv."""
    rounds = dedupe([e for e in events if e.date[:10] > period_end[:10]])
    confirmed = announced = credit = 0.0
    for event in rounds:
        if currency and event.currency and event.currency != currency:
            continue  # annan valuta kan inte läggas på rapportens kassa
        if event.status == "completed":
            confirmed += confirmed_cash(event)
            credit += undrawn_credit(event)
        elif event.amount is not None and event.type not in NO_RAISE_TYPES:
            announced += event.amount
    listing = [{**asdict(e), "amount": e.amount, "cash": confirmed_cash(e) if e.status == "completed" else 0.0}
               for e in sorted(rounds, key=lambda e: e.date, reverse=True)]
    return confirmed, announced, listing, credit
