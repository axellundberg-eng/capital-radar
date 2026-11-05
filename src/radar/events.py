"""Strukturella händelser efter rapporten: omvänt förvärv, fusion, bud, avnotering m.m.

Rapportens kassa och burn säger inget om runway om bolaget har bytt skepnad efter
rapporten (t.ex. ett litet skal som slutfört ett omvänt förvärv). Därför läser vi
rubriker efter periodslut, ber modellen tolka träffarna och lagrar händelserna.

Lagring:
- `company_events`: en rad per tolkad händelse och pressmeddelande.
- `news.events_status`: NULL = aldrig bedömd för strukturella händelser, `checked` =
  bedömd utan fynd, `structural` = bedömd med fynd, `failed` = misslyckad (försöks om
  med --retry-failed). Kolumnen är skild från `news.status` (finansieringssteget), så
  samma nyhet bedöms aldrig två gånger av något av stegen.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from .extract import ChatClient, ExtractionError, json_object, parse_date
from .financing import build_message

TYPES = ("reverse_acquisition", "merger", "acquisition", "name_change", "public_offer", "delisting",
         "bankruptcy", "reorganisation", "liquidation", "spin_off", "other")
STATUSES = ("announced", "completed", "cancelled")
# Händelser som gör att rapportens kassa och burn inte längre beskriver bolaget. Ett vanligt
# förvärv ingår inte: det visas som händelse men flyttar inte bolaget till egen kategori.
CHANGE_TYPES = ("reverse_acquisition", "merger", "spin_off", "reorganisation",
                "liquidation", "bankruptcy")
WINDOW_DAYS = 120
RANK = {"announced": 1, "completed": 2}

STRUCTURAL = re.compile(
    r"omvänt förvärv|omvända förvärv|reverse (?:take-?over|acquisition|merger)"
    r"|fusion|merger|\bmerge[sd]?\b|sammanslagning|samgående"
    r"|namnbyte|byter namn|ändrar namn|name change|change(?:s|d)? (?:its |the )?(?:company )?name|renam(?:e|es|ed|ing)"
    r"|uppköpserbjudande|offentligt erbjudande|\bbud\b|budet|takeover|take-over|public offer(?!ing)|tender offer|bud på"
    r"|avnotering|avnoteras|delist|listing change|change of listing|byte av (?:lista|handelsplats|marknadsplats)"
    r"|konkurs|bankrupt|insolven"
    r"|rekonstruktion|reorgani[sz]ation"
    r"|likvidation|liquidat"
    r"|förvärv|förvärvar|förvärvat|acqui(?:re|res|red|sition)"
    r"|uppdelning(?! av aktie)|spin-?off|avknoppning", re.IGNORECASE)
NOISE = re.compile(
    r"egna aktier|own shares|återköp|buy-?back|insyn|PDMR|managers.? transactions|anmälan om transaktion"
    r"|ledande befattningshavare|förvärv av aktier i bolaget", re.IGNORECASE)
REPORT = re.compile(r"rapport|bokslutskommuniké|interim report|year-end report|quarterly report", re.IGNORECASE)


def looks_structural(title: str | None) -> bool:
    """Billigt förfilter på rubriken: bara ord för strukturella händelser, inte rapporter eller brus."""
    text = title or ""
    if REPORT.search(text) or NOISE.search(text):
        return False
    return bool(STRUCTURAL.search(text))


PROMPT = """Du läser ett pressmeddelande från ett börsnoterat bolag och avgör om det beskriver en
strukturell händelse som ändrar vilket bolag investeraren faktiskt äger: omvänt förvärv, fusion,
förvärv av ett bolag eller en rörelse, namnbyte, offentligt uppköpserbjudande, avnotering eller byte
av handelsplats, konkurs, rekonstruktion, likvidation eller uppdelning/avknoppning.

Svara med enbart ett JSON-objekt:
{
  "is_structural": true eller false,
  "events": [
    {"type": "reverse_acquisition | merger | acquisition | name_change | public_offer | delisting | bankruptcy | reorganisation | liquidation | spin_off | other",
     "status": "announced | completed | cancelled",
     "date": "YYYY-MM-DD", "counterparty": "namn eller null", "quote": "kort citat"}
  ]
}

Regler:
- Ett omvänt förvärv (bolaget förvärvas av ett större onoterat bolag via apportemission) gör att
  rapportens kassa och burn är inaktuella: type "reverse_acquisition".
- type "acquisition" gäller bara förvärv av ett bolag eller en rörelse av betydelse, inte köp av
  egna aktier, insynsaffärer eller små tillgångar.
- status "completed": genomfört, slutfört, villkoren uppfyllda eller registrerat. "announced": beslutat,
  föreslaget eller villkorat. "cancelled": avbrutet eller återkallat.
- date är händelsens datum; annars pressmeddelandets datum. counterparty är motparten (köpare, målbolag,
  nytt namn), annars null. Gissa inte.
- Om texten inte beskriver en sådan händelse: {"is_structural": false, "events": []}.
"""


@dataclass(frozen=True)
class StructuralEvent:
    type: str
    status: str
    date: str
    counterparty: str | None = None
    quote: str | None = None
    title: str | None = None
    url: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text[:200] if text and text.lower() not in ("null", "none") else None


def parse_events(raw: str, published_at: str | None) -> list[StructuralEvent]:
    """Tolka svaret; trasigt svar är ett fel (ExtractionError), ingen händelse är en tom lista."""
    data = json_object(raw)
    if not data.get("is_structural"):
        return []
    published = (published_at or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    events = []
    for item in data.get("events") or []:
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            continue
        kind = item.get("type")
        events.append(StructuralEvent(kind if kind in TYPES else "other", item["status"],
                                      parse_date(item.get("date")) or published, _text(item.get("counterparty")),
                                      (item.get("quote") or "")[:300] or None))
    return events


def ask(client: ChatClient, message: str, published_at: str | None, *, attempts: int = 2
        ) -> tuple[list[StructuralEvent], dict[str, int]]:
    """Ett nytt försök vid trasigt svar, sedan fel."""
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    last: ExtractionError | None = None
    for _ in range(max(1, attempts)):
        try:
            raw, usage = client.complete(PROMPT, message)
            for key in usage_total:
                usage_total[key] += usage.get(key, 0)
            return parse_events(raw, published_at), usage_total
        except ExtractionError as error:
            last = error
    raise last  # type: ignore[misc]


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)


def _merge(group: list[StructuralEvent]) -> StructuralEvent | None:
    """En händelse per grupp: bästa status; avbruten utan genomförd tas bort."""
    if any(e.status == "cancelled" for e in group) and not any(e.status == "completed" for e in group):
        return None
    best = max(group, key=lambda e: (RANK.get(e.status, 0), e.date))
    counterparty = best.counterparty or next((e.counterparty for e in group if e.counterparty), None)
    return StructuralEvent(best.type, best.status, best.date, counterparty, best.quote, best.title, best.url)


def dedupe(events: list[StructuralEvent]) -> list[StructuralEvent]:
    """Samma typ inom 120 dagar är samma händelse (annonserad + genomförd blir en)."""
    groups: list[list[StructuralEvent]] = []
    for event in sorted(events, key=lambda e: e.date):
        target = next((g for g in groups if g[0].type == event.type
                       and any(_days(event.date, other.date) <= WINDOW_DAYS for other in g)), None)
        if target is None:
            groups.append([event])
        else:
            target.append(event)
    return [merged for merged in (_merge(g) for g in groups) if merged is not None]


def summarize(events: list[StructuralEvent], period_end: str) -> tuple[list[dict], list[str]]:
    """(händelser efter periodslut som listor, flaggor): structural_change, public_offer, delisting."""
    listing = [asdict(e) for e in sorted(dedupe([e for e in events if e.date[:10] > period_end[:10]]),
                                         key=lambda e: e.date, reverse=True)]
    flags = []
    if any(e["type"] in CHANGE_TYPES for e in listing):
        flags.append("structural_change")
    if any(e["type"] == "public_offer" for e in listing):
        flags.append("public_offer")
    if any(e["type"] == "delisting" for e in listing):
        flags.append("delisting")
    return listing, flags


def load_events(connection: sqlite3.Connection, company_id: int) -> list[StructuralEvent]:
    rows = connection.execute(
        """SELECT e.type, e.status, e.event_date, e.counterparty, e.quote, n.title, n.url
           FROM company_events e JOIN news n ON n.document_id = e.news_document_id
           WHERE e.company_id=?""", (company_id,)).fetchall()
    return [StructuralEvent(*tuple(row)) for row in rows]
