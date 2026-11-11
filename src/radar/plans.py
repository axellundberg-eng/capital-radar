"""Signaler om kommande kapitalanskaffning som ännu inte är en annonserad emission.

Fem slag, alla med reguljära uttryck (inga AI-anrop):

- `stamma_issue`: en bolagsstämma har beslutat om en emission (eller godkänt styrelsens
  beslut). Ett rent bemyndigande räknas inte, det har nästan alla bolag.
- `egm_called`: kallelse till extra bolagsstämma med emission, emissionsbemyndigande, minskning av
  aktiekapitalet eller ändrade aktiekapitalgränser på dagordningen. Kommer före stämmobeslutet.
- `plan`: bolaget säger att det ska ta in kapital, gärna med tidpunkt ("under första
  kvartalet 2027"). Kräver ett tydligt avsiktsverb, eller ett svagare verb ("utvärderar",
  "behöver") tillsammans med en tidsangivelse. Meningar om genomförda emissioner hoppas över.
- `going_concern`: senaste rapporten anger väsentlig osäkerhet om fortsatt drift.
- `evaluating`: bolaget utvärderar ny finansiering utan tidpunkt (svagare, visas som sammanhang).

Källor: sidorna i varje bolags senaste rapport (redan sparade) och sparad text för
pressmeddelanden de senaste 12 månaderna. Kallelsernas text hämtas en gång (`fetch_notices`).
Tabellen byggs om helt vid varje körning. En signal släcks när bolaget därefter genomfört en emission.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Callable

ACTIVE_DAYS = 270
# En stämmogodkänd emission genomförs normalt inom ett par månader: äldre beslut utan registrerat
# utfall beror oftare på luckor i datan än på en kapitalanskaffning som fortfarande pågår.
ACTIVE_DAYS_BY_KIND = {"stamma_issue": 120, "egm_called": 120, "plan": ACTIVE_DAYS,
                       "going_concern": ACTIVE_DAYS, "evaluating": ACTIVE_DAYS}
# Stämman godkänner ofta ett styrelsebeslut som redan registrerats med styrelsens datum, så en
# genomförd emission en tid före signalen släcker den också.
LOOKBACK_DAYS = {"stamma_issue": 90, "egm_called": 30, "plan": 30, "going_concern": 15, "evaluating": 30}
NEWS_DAYS = 365
NOTICE_DAYS = 120
QUOTE_CHARS = 320
EQUITY = ("rights_issue", "directed_issue", "warrants", "convertible")
KIND_ORDER = {"stamma_issue": 0, "egm_called": 1, "plan": 2, "going_concern": 3, "evaluating": 4}

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
        r"|behov\s+av|evaluat(?:es|ing|e)|exploring|considering|reviewing|needs?\s+(?:to|additional|further)|in\s+need\s+of"
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
                       r"|incentive|employee|kvittning|set-?off|apportemission|in\s+kind|sammanläggning|split"
                       r"|senior\s+executives"
                       # 16 kap. aktiebolagslagen: emission till styrelse, ledning och närstående, oftast en del av en
                       # större emission som redan offentliggjorts.
                       r"|16\s*kap\w*\.?\s+(?:i\s+)?aktiebolagslagen|chapter\s+16\s+of\s+the\s+swedish\s+companies\s+act", re.I)
# Rubriker om en genomförd emission eller ett utfall. Ett stämmobeslut eller en kallelse kort före eller
# när som helst efter en sådan rubrik gäller en emission som redan är gjord (även om händelsen saknas i
# finansieringshistoriken, t.ex. för nyheter före rapportperioden som aldrig AI-granskats).
DONE_TITLE = re.compile(
    r"\bgenomför\s+(?:en\s+)?(?:\w+\s+)?(?:ny)?emission|\bhar\s+(?:framgångsrikt\s+)?genomfört\s+(?:en\s+)?\w*\s*(?:ny)?emission"
    r"|utfall(?:et)?\s+(?:i|av|från)\b|offentliggör\s+utfall|övertecknad"
    r"|announces?\s+(?:the\s+)?outcome|carries\s+out\s+(?:a\s+)?\w*\s*(?:share\s+)?issue"
    r"|(?:successfully\s+)?complete[sd]?\s+(?:an?\s+)?(?:\w+\s+)?(?:directed\s+)?(?:share\s+|rights\s+)?issue"
    r"|issue\s+(?:was\s+)?oversubscribed", re.I)
DONE_TITLE_DAYS = 45
# Stämman godkänner en del av en emission som redan är tecknad eller genomförd (t.ex. aktier till
# styrelse och ledning i en riktad emission som offentliggjorts tidigare): ingen ny kapitalanskaffning.
ALREADY_DONE = re.compile(r"(?:har\s+)?tecknats\s+och\s+tilldelats|har\s+tecknats|genomfördes\s+och\s+offentliggjordes"
                          r"|som\s+(?:ett\s+)?led\s+i\s+(?:det|den)\s+(?:accelererade|genomförda|tidigare)"
                          r"|subscribed\s+(?:for\s+)?and\s+allotted|has\s+been\s+subscribed|was\s+carried\s+out\s+and\s+announced"
                          r"|as\s+part\s+of\s+the\s+(?:accelerated|completed|previously)", re.I)
# Kallelse till extra bolagsstämma (rubrik) och emissionsärenden på dess dagordning (text).
EGM_TITLE = re.compile(r"kallelse\s+till\s+extra\s+bolagsstämma|kallelse\s+till\s+extra\s+stämma"
                       r"|notice\s+(?:of|to)\s+(?:attend\s+)?(?:the\s+|an\s+)?extra\s*ordinary\s+general\s+meeting"
                       r"|notice\s+(?:of|to)\s+(?:attend\s+)?(?:the\s+|an\s+)?egm", re.I)
EGM_AGENDA = re.compile(
    r"(?:beslut|förslag\s+till\s+beslut|resolution|proposal)[^.]{0,80}?"
    r"(?:(?:godkännande\s+av\s+styrelsens\s+beslut\s+om\s+)?(?:nyemission|företrädesemission|riktad\w*\s+(?:ny)?emission"
    r"|emission\s+av\s+(?:units|aktier|nya\s+aktier))"
    r"|bemyndigande[^.]{0,80}?(?:nyemission|emission|emittera)"
    r"|minskning\s+av\s+aktiekapital\w*"
    r"|ändring\s+av\s+bolagsordning\w*[^.]{0,60}?(?:gräns\w*|aktiekapital\w*|antal\s+aktier)"
    r"|(?:approval\s+of\s+the\s+board['’]s\s+resolution\s+(?:on|regarding)\s+(?:a\s+)?)?(?:rights\s+issue|new\s+(?:share\s+)?issue"
    r"|directed\s+(?:share\s+)?issue|issue\s+of\s+(?:units|new\s+shares|shares))"
    r"|authori[sz]ation[^.]{0,80}?issu\w+"
    r"|reduction\s+of\s+the\s+share\s+capital"
    r"|amendment\s+of\s+the\s+articles[^.]{0,60}?(?:limits?|share\s+capital|number\s+of\s+shares))", re.I)
GOING_CONCERN = re.compile(
    r"väsentlig\w*\s+osäkerhet\w*[^.]{0,220}fortsatt\s+drift|fortsatt\s+drift[^.]{0,220}väsentlig\w*\s+osäkerhet"
    r"|material\s+uncertaint\w*[^.]{0,220}going\s+concern|going\s+concern[^.]{0,220}material\s+uncertaint\w*", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ0-9\"“])")
# Avstavning vid radbrytning i PDF- och webbtext: "befatt-ningshavare" -> "befattningshavare".
LINE_HYPHEN = re.compile(r"(?<=[a-zåäö])-(?=[a-zåäö])")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _flat(text: str | None) -> str:
    return LINE_HYPHEN.sub("", " ".join((text or "").replace("­", "").split()))


def _sentence_at(flat: str, start: int, end: int) -> str:
    """Meningen runt en träff, högst QUOTE_CHARS tecken."""
    left = max(flat.rfind(". ", 0, start) + 2, 0)
    right = flat.find(". ", end)
    return flat[left: (right + 1) if right >= 0 else len(flat)][:QUOTE_CHARS]


def find_stamma_issue(text: str | None) -> dict | None:
    """Stämmobeslut om en emission (inte ett bemyndigande), annars None."""
    flat = _flat(text)
    for match in STAMMA.finditer(flat):
        # Uteslutningar gäller meningen där beslutet står, inte nästa punkt i kommunikén (t.ex. ett
        # incitamentsprogram eller en sammanläggning som beslutades vid samma stämma).
        end = flat.find(". ", match.end())
        window = flat[match.start(): min(end + 1 if end >= 0 else len(flat), match.end() + 300)]
        context = flat[match.start(): match.end() + 500]   # beslutets stycke: redan tecknad eller genomförd?
        if AUTHORIZATION.search(match.group(0)) or NOT_RAISE.search(window) or ALREADY_DONE.search(context):
            continue
        return {"kind": "stamma_issue", "timing": None, "quote": flat[match.start(): match.start() + QUOTE_CHARS]}
    return None


def find_egm_issue(title: str | None, text: str | None) -> dict | None:
    """Kallelse till extra bolagsstämma med ett emissionsärende på dagordningen, annars None."""
    if not EGM_TITLE.search(title or ""):
        return None
    flat = _flat(text)
    for match in EGM_AGENDA.finditer(flat):
        end = flat.find(". ", match.end())
        window = flat[match.start(): min(end + 1 if end >= 0 else len(flat), match.end() + 300)]
        if NOT_RAISE.search(window):
            continue   # incitamentsprogram, kvittning eller sammanläggning är ingen kapitalanskaffning
        # Dagordningar saknar ofta punkter mellan ärendena: citera från ärendet, inte från meningens början.
        return {"kind": "egm_called", "timing": None, "quote": flat[match.start(): match.start() + QUOTE_CHARS]}
    return None


def find_going_concern(text: str | None) -> dict | None:
    """Väsentlig osäkerhet om fortsatt drift i rapporttexten, annars None."""
    flat = _flat(text)
    match = GOING_CONCERN.search(flat)
    if not match:
        return None
    return {"kind": "going_concern", "timing": None, "quote": _sentence_at(flat, match.start(), match.end())}


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


def notice_candidates(connection: sqlite3.Connection, today: str, limit: int) -> list[sqlite3.Row]:
    """Kallelser till extra stämma de senaste NOTICE_DAYS dagarna som saknar sparad text."""
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=NOTICE_DAYS)).isoformat()
    rows = connection.execute(
        """SELECT document_id, title, url FROM news WHERE text IS NULL AND url IS NOT NULL
           AND substr(published_at, 1, 10) >= ? ORDER BY published_at DESC""", (cutoff,)).fetchall()
    return [row for row in rows if EGM_TITLE.search(row["title"] or "")][:limit]


def fetch_notices(connection: sqlite3.Connection, settings, *, today: str | None = None, limit: int = 40,
                  fetch: Callable | None = None, throttle=None, log: Callable[[str], None] = lambda _l: None
                  ) -> dict[str, int]:
    """Hämta texten för kallelser till extra stämma (en gång per kallelse) så att dagordningen kan läsas."""
    from . import mfn
    from .fetch import fetch_text

    fetch = fetch or (lambda url: fetch_text(url, timeout=min(settings.http_timeout, 60.0),
                                             user_agent=settings.user_agent))
    throttle = throttle or mfn.Throttle()
    rows = notice_candidates(connection, today or date.today().isoformat(), limit)
    counts = {"candidates": len(rows), "fetched": 0, "failed": 0}
    for row in rows:
        throttle.wait()
        try:
            text = fetch(row["url"])
        except Exception as error:  # noqa: BLE001 - en sida får inte stoppa övriga
            counts["failed"] += 1
            log(f"kallelse {row['title'][:60]}: {error}")
            continue
        with connection:
            connection.execute("UPDATE news SET text=? WHERE document_id=? AND text IS NULL",
                               (text, row["document_id"]))
        counts["fetched"] += 1
    return counts


def run(connection: sqlite3.Connection, *, today: str | None = None) -> dict[str, int]:
    """Bygg om tabellen `financing_signals` ur rapportsidor och sparad nyhetstext."""
    today = today or date.today().isoformat()
    rows: list[tuple] = []
    for report in _latest_reports(connection):
        text = " ".join(page for (page,) in connection.execute(
            "SELECT text FROM report_pages WHERE document_id=? ORDER BY page", (report["document_id"],)))
        for found in (find_plan(text), find_going_concern(text)):
            if found:
                rows.append((report["company_id"], found["kind"], "report", report["document_id"],
                             (report["published_at"] or "")[:10], found["timing"], found["quote"], report["url"]))
    cutoff = (date.fromisoformat(today[:10]) - timedelta(days=NEWS_DAYS)).isoformat()
    done_dates: dict[int, list[str]] = {}
    for company_id, title, published in connection.execute(
            "SELECT company_id, title, substr(published_at, 1, 10) FROM news WHERE substr(published_at, 1, 10) >= ?",
            ((date.fromisoformat(cutoff) - timedelta(days=DONE_TITLE_DAYS)).isoformat(),)):
        if DONE_TITLE.search(title or ""):
            done_dates.setdefault(company_id, []).append(published)

    def already_done(company_id: int, day: str) -> bool:
        since = (date.fromisoformat(day) - timedelta(days=DONE_TITLE_DAYS)).isoformat()
        return any(d >= since for d in done_dates.get(company_id, []))

    for news in connection.execute(
            """SELECT document_id, company_id, title, published_at, url, text FROM news
               WHERE text IS NOT NULL AND substr(published_at, 1, 10) >= ?""", (cutoff,)):
        egm = find_egm_issue(news["title"], news["text"])
        # En kallelse beskriver förslag, inte beslut eller planer: läs den bara som kallelse.
        found_all = (egm,) if egm or EGM_TITLE.search(news["title"] or "") else (
            find_stamma_issue(news["text"]), find_plan(news["text"]))
        for found in found_all:
            if found and found["kind"] in ("stamma_issue", "egm_called") and already_done(
                    news["company_id"], (news["published_at"] or "")[:10]):
                continue
            if found:
                rows.append((news["company_id"], found["kind"], "news", news["document_id"],
                             (news["published_at"] or "")[:10], found["timing"], found["quote"], news["url"]))
    with connection:
        connection.execute("DELETE FROM financing_signals")
        connection.executemany(
            """INSERT OR IGNORE INTO financing_signals(company_id, kind, source, document_id, published_at,
               timing, quote, url, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(*row, _now()) for row in rows])
    counts = {"signals": len(rows), "companies": len({row[0] for row in rows})}
    for kind in KIND_ORDER:
        counts[kind] = sum(row[1] == kind for row in rows)
    return counts


def active_all(connection: sqlite3.Connection, history: dict[int, list[dict]], today: str) -> dict[int, list[dict]]:
    """Aktuella signaler per bolag, en per slag, ordnade efter vikt (KIND_ORDER).

    En signal är aktuell när den inte är för gammal (`ACTIVE_DAYS_BY_KIND`) och ingen genomförd emission
    finns efter signalen eller kort före den (`LOOKBACK_DAYS`). `history` är qualify.load_history
    (händelser med granskningens rättelser). Inom samma slag går nyare före äldre och med tidpunkt först."""
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
    result: dict[int, list[dict]] = {}
    seen: set[tuple[int, str]] = set()
    for row in rows:
        key = (row["company_id"], row["kind"])
        if key in seen:
            continue
        since = (date.fromisoformat(row["published_at"])
                 - timedelta(days=LOOKBACK_DAYS.get(row["kind"], 30))).isoformat()
        raised = any(e.get("type") in EQUITY and e.get("status") == "completed"
                     and (e.get("event_date") or "")[:10] >= since
                     for e in history.get(row["company_id"], []))
        if not raised:
            seen.add(key)
            result.setdefault(row["company_id"], []).append(
                {k: row[k] for k in ("kind", "source", "published_at", "timing", "quote", "url")})
    return result


def active(connection: sqlite3.Connection, history: dict[int, list[dict]], today: str) -> dict[int, dict]:
    """Den viktigaste aktuella signalen per bolag (för bannern)."""
    return {company: signals[0] for company, signals in active_all(connection, history, today).items()}
