"""Signaler om kommande kapitalanskaffning som ännu inte är en annonserad emission.

Två slag, båda med reguljära uttryck (inga AI-anrop):

- `stamma_issue`: en bolagsstämma har beslutat om en emission (eller godkänt styrelsens
  beslut). Ett rent bemyndigande räknas inte, det har nästan alla bolag.
- `plan`: bolaget säger att det ska ta in kapital, gärna med tidpunkt ("under första
  kvartalet 2027"). Kräver ett tydligt avsiktsverb, eller ett svagare verb ("utvärderar",
  "behöver") tillsammans med en tidsangivelse. Meningar om genomförda emissioner hoppas över.

Källor: sidorna i varje bolags senaste rapport (redan sparade) och sparad text för
pressmeddelanden de senaste 12 månaderna. Tabellen byggs om helt vid varje körning.
En signal är aktiv i 270 dagar och släcks när bolaget därefter genomfört en emission.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

ACTIVE_DAYS = 270
# En stämmogodkänd emission genomförs normalt inom ett par månader: äldre beslut utan registrerat
# utfall beror oftare på luckor i datan än på en kapitalanskaffning som fortfarande pågår.
ACTIVE_DAYS_BY_KIND = {"stamma_issue": 120, "plan": ACTIVE_DAYS, "evaluating": ACTIVE_DAYS}
# Stämman godkänner ofta ett styrelsebeslut som redan registrerats med styrelsens datum, så en
# genomförd emission en tid före signalen släcker den också.
LOOKBACK_DAYS = {"stamma_issue": 90, "plan": 30, "evaluating": 30}
NEWS_DAYS = 365
QUOTE_CHARS = 320
EQUITY = ("rights_issue", "directed_issue", "warrants", "convertible")

RAISE = (r"kapitalanskaffning\w*|kapitaltillskott|nyemission\w*|företrädesemission\w*|riktad\w*\s+(?:ny)?emission\w*"
         r"|emission\s+av\s+(?:units|aktier|nya aktier)|(?:ytterligare|ny|extern|fortsatt|långsiktig)\s+finansiering"
         r"|tillföra\s+(?:bolaget\s+)?(?:nytt\s+|ytterligare\s+)?kapital|ta\s+in\s+(?:nytt\s+|ytterligare\s+)?kapital"
         r"|säkra\s+(?:bolagets\s+)?(?:fortsatta\s+|långsiktiga\s+)?finansiering|finansieringslösning"
         r"|rais\w+\s+(?:additional\s+|new\s+|further\s+)?(?:capital|funds|equity)|capital\s+rais\w+"
         r"|rights\s+issue|share\s+issue|new\s+issue|directed\s+issue|(?:additional|further|new)\s+(?:financing|funding)"
         r"|secure\s+(?:additional\s+|further\s+|long-term\s+)?(?:financing|funding)"
         r"|finansieringsalternativ\w*|finansieringsbehov\w*|kapitalbehov\w*|(?:ytterligare|nytt)\s+(?:rörelse)?kapital"
         r"|financing\s+(?:alternatives|options)|funding\s+(?:needs|requirements)|capital\s+needs")
STRONG = (r"avser\s+att|planerar\s+att|har\s+för\s+avsikt\s+att|förbereder\s+(?:en|ett)|siktar\s+på\s+att"
          r"|kommer\s+att\s+(?:genomföra|ta\s+in|söka|behöva)|kommer\s+att\s+behövas|ska\s+(?:genomföra|ta\s+in)"
          r"|räknar\s+med\s+att\s+(?:genomföra|ta\s+in)|intends?\s+to|plans?\s+to|planning\s+to"
          r"|is\s+preparing|will\s+(?:carry\s+out|raise|conduct|seek|need)|expects?\s+to\s+(?:raise|carry\s+out)")
WEAK = (r"utvärderar|utvärderas|att\s+utvärdera|överväger|undersöker|ser\s+över|arbetar\s+(?:aktivt\s+)?(?:med|för)|behöver|är\s+i\s+behov\s+av"
        r"|behov\s+av|evaluat(?:es|ing|e)|exploring|considering|reviewing|needs?\s+(?:to|additional|further)|in\s+need\s+of"
        r"|will\s+be\s+required|krävs")
# Mjuka formuleringar som inte är ett besked: negationer och riskavsnittens "kan komma att behöva".
HEDGE = (r"inte\s+(?:något|har\s+något)\s+behov|inget\s+behov|utan\s+(?:behov\s+av\s+)?kapitaltillskott|no\s+need|not\s+need"
         r"|may\s+need|might\s+need|could\s+need|kan\s+(?:komma\s+att\s+)?behöva|cannot\s+be\s+excluded|inte\s+uteslutas"
         r"|portföljbolag|portfolio\s+compan|onoterade|värdering|valuation|vägledning|betalnings-\s+och\s+finansieringslösning|slutkund|customers?['’]?\s+financing")
DONE = (r"genomförde|genomförd\b|har\s+genomfört|genomfördes|genomförts|tillfördes|tillförts|slutförde|avslutades|registrerades"
        r"|övertecknad|utfall|completed|carried\s+out|was\s+(?:oversubscribed|registered)|raised\s+(?:approximately|SEK|MSEK)"
        # Rapportkalendrar, redovisningstext, riskavsnitt och villkor i en redan annonserad emission.
        r"|kvartalsrapport|delårsrapport|bokslutskommuniké|årsredovisning|interim\s+report|quarterly\s+report"
        r"|annual\s+report|year-end\s+report|emissionskostnad|issue\s+costs|kassaflöde|cash\s+flow"
        r"|risk\s+att|risken\s+att|risk\s+that|riskfaktor|risk\s+factor|överteckning|övertilldelning|over-?allotment")
QUARTER = r"(?:första|andra|tredje|fjärde|1:a|2:a|3:e|4:e)\s+kvartalet"
TIMING = re.compile(
    rf"(?:under|i|före|innan|senast\s+under|senast\s+i|in|during|by|before)\s+(?:det\s+|the\s+)?{QUARTER}(?:\s+20\d\d)?"
    r"|\b(?:Q[1-4]|H[12])\s*20\d\d\b|\b(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?20\d\d"
    r"|(?:under|i|in|during)\s+(?:våren|hösten|sommaren|vintern|början\s+av|slutet\s+av|mitten\s+av|första\s+halvåret"
    r"|andra\s+halvåret|the\s+first\s+half\s+of|the\s+second\s+half\s+of|early|late|spring|autumn|fall)\s+(?:år\s+)?20\d\d"
    r"|(?:under|i|in|during)\s+(?:år\s+)?20\d\d\b"
    r"|(?:inom|within)\s+(?:de\s+|the\s+)?(?:närmaste|kommande|next|coming)\s+(?:\d+|tre|sex|nio|tolv|three|six|nine|twelve)"
    r"\s+(?:månader\w*|months)", re.I)
PLAN_RAISE, PLAN_STRONG, PLAN_WEAK, PLAN_DONE, PLAN_HEDGE = (
    re.compile(p, re.I) for p in (RAISE, STRONG, WEAK, DONE, HEDGE))
KIND_ORDER = {"stamma_issue": 0, "plan": 1, "evaluating": 2}

STAMMA = re.compile(
    r"(?:bolags)?stämman\s+(?:beslutade|beslöt|godkände|fattade\s+beslut)[^.]{0,200}?"
    r"(?:företrädesemission|nyemission|riktad\w*\s+(?:ny)?emission\s+av\s+(?:units|aktier|nya\s+aktier)|emission\s+av\s+(?:units|aktier|nya\s+aktier)"
    r"|styrelsens\s+beslut\s+om\s+(?:en\s+)?(?:\w+\s+)?emission)"
    r"|(?:general\s+)?meeting\s+(?:resolved|approved|decided)[^.]{0,200}?"
    r"(?:rights\s+issue|new\s+(?:share\s+)?issue|directed\s+(?:share\s+)?issue|issue\s+of\s+(?:units|new\s+shares|shares)"
    r"|board['’]s\s+(?:resolution|decision)\s+(?:on|regarding)\s+(?:a\s+|the\s+)?(?:\w+\s+)?issue)", re.I)
AUTHORIZATION = re.compile(r"bemyndig|authori[sz]", re.I)
# Incitamentsprogram och kvittningar är emissioner men ingen kapitalanskaffning.
NOT_RAISE = re.compile(r"incitament|personaloption|optionsprogram|nyckelpersoner|anställda|ledande\s+befattningshavare"
                       r"|incentive|employee|kvittning|set-?off|apportemission|in\s+kind|sammanläggning|split", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ0-9\"“])")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _flat(text: str | None) -> str:
    return " ".join((text or "").replace("­", "").split())


def find_stamma_issue(text: str | None) -> dict | None:
    """Stämmobeslut om en emission (inte ett bemyndigande), annars None."""
    flat = _flat(text)
    for match in STAMMA.finditer(flat):
        window = flat[match.start(): match.end() + 250]
        if AUTHORIZATION.search(match.group(0)) or NOT_RAISE.search(window):
            continue
        return {"kind": "stamma_issue", "timing": None, "quote": flat[match.start(): match.start() + QUOTE_CHARS]}
    return None


def find_plan(text: str | None) -> dict | None:
    """Tydligaste meningen om att bolaget ska ta in eller utvärderar ny finansiering.

    `plan`: avsiktsverb, eller svagare verb med tidsangivelse. `evaluating`: svagare verb utan
    tidpunkt ("utvärderar finansieringsalternativ", "ytterligare finansiering krävs"). Bästa träffen
    vinner: plan med tidpunkt, plan, sedan evaluating."""
    best, rank = None, 9
    for sentence in SENTENCE.split(_flat(text)):
        if (len(sentence) > 600 or sentence.count("•") > 1 or not PLAN_RAISE.search(sentence)
                or PLAN_DONE.search(sentence) or PLAN_HEDGE.search(sentence)):
            continue
        timing = TIMING.search(sentence)
        strong, weak = PLAN_STRONG.search(sentence), PLAN_WEAK.search(sentence)
        if strong or (weak and timing):
            kind, level = "plan", (0 if timing else 1)
        elif weak:
            kind, level = "evaluating", 2
        else:
            continue
        if level < rank:
            best, rank = {"kind": kind, "timing": timing.group(0) if timing else None,
                          "quote": sentence[:QUOTE_CHARS]}, level
            if level == 0:
                break  # en mening med avsikt och tidpunkt är det tydligaste beskedet
    return best


def _latest_reports(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT r.document_id, r.company_id, r.published_at, r.pdf_url AS url FROM reports r
           WHERE r.document_id = (SELECT r2.document_id FROM reports r2 JOIN runway_results x
                                  ON x.document_id = r2.document_id WHERE r2.company_id = r.company_id
                                  ORDER BY r2.published_at DESC, r2.document_id ASC LIMIT 1)""").fetchall()


def run(connection: sqlite3.Connection, *, today: str | None = None) -> dict[str, int]:
    """Bygg om tabellen `financing_signals` ur rapportsidor och sparad nyhetstext."""
    today = today or date.today().isoformat()
    rows: list[tuple] = []
    for report in _latest_reports(connection):
        text = " ".join(page for (page,) in connection.execute(
            "SELECT text FROM report_pages WHERE document_id=? ORDER BY page", (report["document_id"],)))
        found = find_plan(text)
        if found:
            rows.append((report["company_id"], found["kind"], "report", report["document_id"],
                         (report["published_at"] or "")[:10], found["timing"], found["quote"], report["url"]))
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=NEWS_DAYS)).isoformat()
    for news in connection.execute(
            """SELECT document_id, company_id, published_at, url, text FROM news
               WHERE text IS NOT NULL AND substr(published_at, 1, 10) >= ?""", (cutoff,)):
        for found in (find_stamma_issue(news["text"]), find_plan(news["text"])):
            if found:
                rows.append((news["company_id"], found["kind"], "news", news["document_id"],
                             (news["published_at"] or "")[:10], found["timing"], found["quote"], news["url"]))
    with connection:
        connection.execute("DELETE FROM financing_signals")
        connection.executemany(
            """INSERT OR IGNORE INTO financing_signals(company_id, kind, source, document_id, published_at,
               timing, quote, url, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(*row, _now()) for row in rows])
    counts = {"signals": len(rows), "companies": len({row[0] for row in rows}),
              "stamma_issue": sum(row[1] == "stamma_issue" for row in rows),
              "plan": sum(row[1] == "plan" for row in rows),
              "evaluating": sum(row[1] == "evaluating" for row in rows)}
    return counts


def active(connection: sqlite3.Connection, history: dict[int, list[dict]], today: str) -> dict[int, dict]:
    """Aktuell signal per bolag: inte för gammal (`ACTIVE_DAYS_BY_KIND`) och ingen genomförd emission
    efter signalen eller kort före den (`LOOKBACK_DAYS`).

    `history` är qualify.load_history (händelser med granskningens rättelser). Stämmobeslut går före
    planer och planer före utvärderingar; inom samma slag nyare före äldre och med tidpunkt först."""
    day = date.fromisoformat(today[:10])
    cutoff = (day - timedelta(days=max(ACTIVE_DAYS_BY_KIND.values()))).isoformat()
    try:
        rows = connection.execute(
            "SELECT * FROM financing_signals WHERE published_at >= ?", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    rows = [r for r in rows if r["published_at"] >= (
        day - timedelta(days=ACTIVE_DAYS_BY_KIND.get(r["kind"], ACTIVE_DAYS))).isoformat()]
    rows.sort(key=lambda r: r["published_at"] or "", reverse=True)                 # nyast först ...
    rows.sort(key=lambda r: (KIND_ORDER.get(r["kind"], 9), r["timing"] is None))   # ... inom slag (stabil sortering)
    result: dict[int, dict] = {}
    for row in rows:
        if row["company_id"] in result:
            continue
        since = (date.fromisoformat(row["published_at"])
                 - timedelta(days=LOOKBACK_DAYS.get(row["kind"], 30))).isoformat()
        raised = any(e.get("type") in EQUITY and e.get("status") == "completed"
                     and (e.get("event_date") or "")[:10] >= since
                     for e in history.get(row["company_id"], []))
        if not raised:
            result[row["company_id"]] = {key: row[key] for key in
                                         ("kind", "source", "published_at", "timing", "quote", "url")}
    return result
