"""Finansieringshändelser i pressmeddelanden: förfilter, AI-tolkning och lagring."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from .extract import ChatClient, ExtractionError, json_object, parse_date, parse_number
from .rounds import Event

TERM_FIELDS = ('subscription_commitments', 'guarantee_amount', 'guarantee_fee_pct', 'guarantors', 'subscription_rate_pct', 'guarantee_utilized', 'issue_price', 'shares_issued', 'shares_before', 'discount_pct', 'exercise_price', 'exercise_start', 'exercise_end')
from .units import parse_unit

TYPES = ("rights_issue", "directed_issue", "bridge_loan", "loan", "credit_facility", "convertible",
         "warrants", "repayment", "other")
STATUSES = ("announced", "completed", "cancelled")

# Starka ord pekar på finansiering; svaga (utfall, teckning, garanti, outcome) räcker inte
# för titlar om årsstämma och kallelse.
STRONG = re.compile(
    r"emission|kapitalanskaffn|finansier|konvertib|teckningsoption|brygglån"
    r"|\b\w*lån(?:et|en|er|ens)?\b|rights issue|directed (?:share )?issue|share issue|private placement"
    r"|\bloans?\b|convertible|warrants?|financing|capital raise"
    r"|amorter|återbetal|inlös|kreditfacilitet|kreditram|checkkredit|kreditlina|credit line|revolving"
    r"|repay|redemption|redeem|credit facility", re.IGNORECASE)
WEAK = re.compile(r"utfall|teckning|garanti|outcome", re.IGNORECASE)
MEETING = re.compile(r"årsstämma|bolagsstämma|kallelse|annual general meeting|\bagm\b|notice of", re.IGNORECASE)
REPORT = re.compile(r"rapport|bokslutskommuniké|interim report|year-end report|quarterly report", re.IGNORECASE)


def looks_like_financing(title: str | None) -> bool:
    """Billigt förfilter på rubriken, så att inte varje nyhet kostar ett AI-anrop."""
    text = title or ""
    if REPORT.search(text):
        return False
    if STRONG.search(text):
        return True
    return bool(WEAK.search(text)) and not MEETING.search(text)


PROMPT = """Du läser ett pressmeddelande från ett börsnoterat bolag och avgör om det beskriver
en finansieringshändelse som påverkar kassan: pengar IN (nyemission, företrädesemission,
riktad emission, lån eller brygglån som tas upp, konvertibel, teckningsoptioner) eller
en beviljad kreditfacilitet (kreditram som bolaget kan dra på) eller
pengar UT (amortering eller återbetalning av lån, inlösen av konvertibel mot kontanter).

Svara med enbart ett JSON-objekt:
{
  "is_financing": true eller false,
  "events": [
    {"type": "rights_issue | directed_issue | bridge_loan | loan | credit_facility | convertible | warrants | repayment | other",
     "status": "announced | completed | cancelled",
     "gross_amount": tal eller null, "net_amount": tal eller null, "drawn_amount": tal eller null,
     "unit": "enheten beloppen anges i, t.ex. SEK, TSEK, MSEK", "currency": "SEK",
     "date": "YYYY-MM-DD", "quote": "kort citat som styrker beloppet och statusen",
     "advisers": "finansiella rådgivare, emissionsinstitut och garanter som nämns, separerade med semikolon, eller null",
     "subscription_commitments": tal eller null, "guarantee_amount": tal eller null,
     "guarantee_fee_pct": tal eller null, "guarantors": "namn; namn" eller null,
     "subscription_rate_pct": tal eller null, "guarantee_utilized": tal eller null,
     "issue_price": tal eller null, "shares_issued": tal eller null, "shares_before": tal eller null,
     "discount_pct": tal eller null, "exercise_price": tal eller null,
     "exercise_start": "YYYY-MM-DD" eller null, "exercise_end": "YYYY-MM-DD" eller null}
  ]
}

Regler:
- type "bridge_loan" = brygglån: kortfristigt lån i väntan på en emission eller annan finansiering
  (pengar in). type "credit_facility" = beviljad kreditram, checkkredit eller kreditlina där bolaget
  kan dra pengar vid behov: ange ramens storlek i gross_amount och, om det står, hur mycket som
  är utnyttjat i drawn_amount (annars null). Ramen är inte pengar in.
- type "loan" = övrigt lån som tas upp (pengar in). type "repayment" = amortering, återbetalning
  av lån eller inlösen av konvertibel mot kontanter (pengar ut). Belopp anges alltid positivt.
- status "completed": pengarna är mottagna eller registrerade, eller ett utfall med inbetalt belopp
  redovisas. status "announced": beslutat, föreslaget eller teckningstiden har inte avslutats.
  status "cancelled": avbrutet eller återkallat.
- Belopp anges i den angivna enheten, inte omräknade. gross_amount före emissionskostnader,
  net_amount efter, bara om det står uttryckligen. Hoppa över ett belopp hellre än att gissa.
- date är händelsens datum (beslut, utfall eller betalning); annars pressmeddelandets datum.
- Om texten inte beskriver finansiering: {"is_financing": false, "events": []}.
- advisers: skriv bara namn som texten uttryckligen nämner (t.ex. "Carnegie; Redeye" för emissionsinstitut
  eller finansiell rådgivare). Om inget namn står: null. Gissa inte.
- Villkor och utfall (bara om texten anger dem, annars null):
  subscription_commitments = teckningsåtaganden och guarantee_amount = garanterat belopp (båda i angiven
  enhet, som beloppen); guarantee_fee_pct = garantiersättning i procent av garanterat belopp;
  guarantors = garanternas namn; subscription_rate_pct = teckningsgrad i procent i utfallet (t.ex. 87);
  guarantee_utilized = hur mycket av garantin som togs i anspråk (angiven enhet).
  issue_price = teckningskurs per aktie (eller per unit) i kronor/valutan, INTE omräknad med enheten;
  shares_issued = antal nya aktier och shares_before = antal aktier före emissionen, exakta antal;
  discount_pct = rabatt i procent som texten anger (mot stängningskurs, VWAP eller teoretiskt värde).
  Teckningsoptioner: exercise_price = lösenpris per aktie, exercise_start/exercise_end = lösenperiod.
- En ny rad per separat finansiering. Gissa inte.
"""


def build_message(title: str, published_at: str | None, text: str) -> str:
    return f"Rubrik: {title}\nPublicerat: {(published_at or '')[:10]}\n\n{text}"


def _scaled(value: float | None, multiplier: float) -> float | None:
    return None if value is None else abs(value) * multiplier


def _event(item: dict, top: dict, published: str) -> Event | None:
    kind, status = item.get("type"), item.get("status")
    if status not in STATUSES:
        return None
    gross = parse_number(item.get("gross_amount"))
    if gross is None:
        gross = parse_number(item.get("amount"))
    net, drawn = parse_number(item.get("net_amount")), parse_number(item.get("drawn_amount"))
    unit = parse_unit(item.get("unit") or top.get("unit"), item.get("currency") or top.get("currency"))
    if (gross is not None or net is not None or drawn is not None) and unit is None:
        raise ExtractionError("enhet och valuta saknas för beloppet")
    multiplier, currency = unit or (1.0, None)
    return Event(kind if kind in TYPES else "other", status, _scaled(gross, multiplier),
                 _scaled(net, multiplier), currency, parse_date(item.get("date")) or published,
                 (item.get("quote") or "")[:300] or None, drawn_amount=_scaled(drawn, multiplier),
                 advisers=(str(item.get("advisers") or "").strip()[:300] or None), **_terms(item, multiplier))


def _terms(item: dict, multiplier: float) -> dict:
    """Villkor och utfall. Belopp skalas med enheten; priser, antal och procent anges som de står."""
    number = lambda key: parse_number(item.get(key))  # noqa: E731
    terms = {key: _scaled(number(key), multiplier)
             for key in ("subscription_commitments", "guarantee_amount", "guarantee_utilized")}
    for key in ("guarantee_fee_pct", "subscription_rate_pct", "discount_pct", "issue_price", "exercise_price",
                "shares_issued", "shares_before"):
        value = number(key)
        terms[key] = abs(value) if value is not None else None
    terms["guarantors"] = str(item.get("guarantors") or "").strip()[:300] or None
    terms["exercise_start"] = parse_date(item.get("exercise_start"))
    terms["exercise_end"] = parse_date(item.get("exercise_end"))
    return terms


def parse_financing(raw: str, published_at: str | None) -> list[Event]:
    """Tolka svaret; tomt eller trasigt svar är ett fel (ExtractionError)."""
    data = json_object(raw)
    if not data.get("is_financing"):
        return []
    published = (published_at or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    events = [_event(item, data, published) for item in data.get("events") or [] if isinstance(item, dict)]
    return [event for event in events if event is not None]


def ask(client: ChatClient, message: str, published_at: str | None, *, attempts: int = 2
        ) -> tuple[list[Event], dict[str, int]]:
    """Ett nytt försök vid tomt eller trasigt svar, sedan fel."""
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    last: ExtractionError | None = None
    for _ in range(max(1, attempts)):
        try:
            raw, usage = client.complete(PROMPT, message)
            for key in usage_total:
                usage_total[key] += usage.get(key, 0)
            return parse_financing(raw, published_at), usage_total
        except ExtractionError as error:
            last = error
    raise last  # type: ignore[misc]


def load_events(connection: sqlite3.Connection, company_id: int) -> list[Event]:
    """Bolagets händelser med granskningens rättelser (dolda händelser tas bort)."""
    from .review import apply_event_row, event_overrides

    rows = connection.execute(
        """SELECT f.*, n.title, n.url
           FROM financing_events f JOIN news n ON n.document_id = f.news_document_id
           WHERE f.company_id=?""", (company_id,)).fetchall()
    overrides = event_overrides(connection, company_id)
    events = []
    for row in rows:
        fixed = apply_event_row(dict(row), overrides.get(row["id"]))
        if fixed is not None:
            events.append(Event(fixed["type"], fixed["status"], fixed["gross_amount"], fixed["net_amount"],
                                fixed["currency"], fixed["event_date"], fixed["quote"], fixed["title"],
                                fixed["url"], fixed["drawn_amount"], fixed["advisers"],
                                **{key: fixed.get(key) for key in TERM_FIELDS}))
    return events
