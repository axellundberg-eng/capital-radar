"""Read-only, deterministic corporate-finance feed over the existing source store.

No migration, fetch, AI invocation, transaction management or persisted cache is
performed here. The caller owns its connection (and, if needed, read snapshot).
All candidate documents are grouped before pagination; there is no news cutoff.
Optional financing-history enrichment is supported but not required.

Linking deliberately favours separate, reviewable rows over an issuer-only merge.
An explicit source URL, instrument series or source-stated decision/subscription
date can link stages. Translations additionally require matching publication time
and title facts. IDs use the lowest source document ID, so newly ingested updates
retain an existing ID; backfilling an older *existing* document can change it.

Amounts are in currency units, never millions. ``amount_basis`` distinguishes
announced_amount, outcome_amount, announced_gross/net, outcome_gross/net,
maximum_proceeds, loan_principal and credit_limit. Unknowns stay null. Structured
event amounts are not trusted without matching source text: evidence may concern
a historical or different transaction, and event rows can be updated in place.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import math
import os
import re
import sqlite3
import threading
from typing import Any, Callable, Iterable

from .relevance import classify_title
from .financing_corrections import effective_overrides
from .transaction_corrections import effective_document_rounds, effective_document_round_links, effective_projection_bindings
from .transaction_value_corrections import effective_transaction_overrides


CATEGORY_LABELS = {
    "rights_issue": "Företrädesemission",
    "directed_issue": "Riktad emission",
    "unit_issue": "Unitemission",
    "warrant_exercise": "Teckningsoptioner",
    "convertible": "Konvertibelfinansiering",
    "bridge_loan": "Brygglån",
    "shareholder_loan": "Aktieägarlån",
    "credit_facility": "Kreditfacilitet",
    "other_equity": "Övrig aktiefinansiering",
    "other_debt": "Övrig lånefinansiering",
}
STATUS_LABELS = {
    "announced": "Offentliggjord", "proposed": "Föreslagen",
    "approved": "Beslutad", "conditional": "Villkorad",
    "preliminary_outcome": "Preliminärt utfall", "completed": "Utfall",
    "subscription_period": "Teckningsperiod",
    "registered": "Registrerad", "cancelled": "Inställd",
    "updated": "Uppdaterad",
}
_PATTERNS = {
    "rights_issue": r"företrädesemission\w*|rights\s+(?:share\s+)?issue\w*|preferential\s+rights",
    "directed_issue": r"riktad\w*\s+(?:ny)?emission\w*|directed\s+(?:share\s+)?issue\w*|private\s+placement\w*",
    "unit_issue": r"unitemission\w*|unit\s+issue\w*|emission\s+av\s+units",
    "warrant_exercise": r"teckningsoption\w*|warrants?\b",
    "convertible": r"konvertib\w*|convertible\w*",
    "bridge_loan": r"brygglån\w*|bryggfinansiering\w*|bridge\s+(?:loans?|financing)",
    "shareholder_loan": r"aktieägarlån\w*|shareholder\s+loans?",
    "credit_facility": r"kreditfacilitet\w*|kreditram\w*|credit\s+facilit\w*|revolving\s+credit",
    "other_equity": r"nyemission\w*|share\s+issue\w*|equity\s+(?:issue|financing)",
    "other_debt": r"låneavtal\w*|lånefinansiering\w*|obligations(?:lån|emission)\w*|loan\s+agreements?|bond\s+issue\w*|debt\s+financing",
}
_RX = {key: re.compile(r"\b(?:" + value + r")", re.I) for key, value in _PATTERNS.items()}
_EVENT_TYPES = {"loan_draw": "other_debt", "loan_commitment": "other_debt"}
_EXCLUDE = re.compile(
    r"\b(?:årsrapport|annual\s+results|financial\s+results|year.end\s+results|"
    r"kallelse|notice\s+(?:of|to)|inbjudan|invitation|webinar|webcast|"
    r"incitamentsprogram|incentive\s+program|personaloption|employee\s+option|"
    r"veckobrev|weekly\s+summary)\b", re.I,
)
_GENERIC = re.compile(r"\b(?:finansiering|kapitalanskaffning|financing|capital\s+raise)\b", re.I)
# Spaced instrument prefixes are case-sensitive: English "to 274,143,719
# shares" and "up to 30 months" are not TO274 and TO30. Compact codes and
# explicit year-series labels may still be written in lower case.
_SERIES = re.compile(
    r"\b(?:TO|KV)\s*\d+(?:\s*[AB])?\b"
    r"|\b(?i:(?:TO|KV)\d+(?:[AB])?)\b"
    r"|(?i:\b(?:serie|series)\s+(\d{4}/\d+(?:\s*[AB])?)\b)"
)
_NUMBER = r"\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)*"
_MONEY = re.compile(
    rf"(?<![\w.,])(?:(?P<currency>SEK|EUR|USD|NOK|DKK|GBP)\s*(?P<prefix>{_NUMBER})"
    rf"(?:\s*(?P<scale>million(?:s)?|miljon(?:er)?|billion(?:s)?|miljard(?:er)?|thousand|tusen))?"
    rf"|(?P<suffix>{_NUMBER})\s*(?P<unit>MSEK|MEUR|MUSD|MNOK|MDKK|MGBP|TSEK|KSEK|"
    rf"(?:miljoner|miljarder|tusen)\s+(?:kronor|SEK|EUR|USD)|SEK|EUR|USD|NOK|DKK|GBP|kronor))\b", re.I,
)
_MONTHS = {
    name: i for i, names in enumerate((
        ("januari", "january"), ("februari", "february"), ("mars", "march"),
        ("april",), ("maj", "may"), ("juni", "june"), ("juli", "july"),
        ("augusti", "august"), ("september",), ("oktober", "october"),
        ("november",), ("december",),
    ), 1) for name in names
}
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b|\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(20\d{2})\b", re.I)
_IDENTITY_CONTEXT = re.compile(
    r"beslut(?:ade|ades|ad|et)?|offentliggjord\w*|kommunicerad\w*|announced|resolved|"
    r"decision|teckningsperiod|subscription\s+period|utnyttjandeperiod|exercise\s+period", re.I,
)
_INSTITUTION = re.compile(r"\b(?:bank\w*|securities|capital|fondkommission|finance|finans|"
                          r"partners|investments?|fonder|fund\w*|carnegie|nordea|dnb|seb|abg)\b", re.I)
_ROLE_WORDS = {
    "Finansiell rådgivare": r"finansiell(?:a)?\s+rådgivare|financial\s+advis[eo]r(?:s)?",
    "Emissionsinstitut": r"emissionsinstitut|issuing\s+agent",
    "Bookrunner": r"(?:sole\s+|joint\s+)?bookrunner",
    "Långivare": r"långivare|lender",
    "Garant": r"garant(?:er)?|guarantor(?:s)?",
}
_ROLE_MAP = {
    "adviser": "Finansiell rådgivare", "advisor": "Finansiell rådgivare",
    "financial_adviser": "Finansiell rådgivare", "financial_advisor": "Finansiell rådgivare",
    "issuer_agent": "Emissionsinstitut", "issuing_agent": "Emissionsinstitut",
    "bookrunner": "Bookrunner", "lender": "Långivare", "guarantor": "Garant",
}
_ROLE_HINT = re.compile('|'.join('(?:'+p+')' for p in _ROLE_WORDS.values())
    + r'|have\s+entered\s+into\s+underwriting\s+commitments|har\s+ingått\s+garantiåtaganden',re.I)
_ROLE_HINTS = {role:re.compile('(?:'+pattern+r')\b',re.I) for role,pattern in _ROLE_WORDS.items()}
_UNDERWRITING_HINT = re.compile(r'have\s+entered\s+into\s+underwriting\s+commitments|har\s+ingått\s+garantiåtaganden\b',re.I)
_TRANSACTION_CACHE: dict[str, tuple[tuple[tuple[int, int], tuple[int, int]], list[dict[str, Any]]]] = {}
_TRANSACTION_CACHE_LOCK = threading.Lock()


def _clean(value: Any) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def _stamp(value: str | None) -> datetime:
    try:
        date = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _rows(connection: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> list[dict]:
    # Cursor-local factory works for default tuple connections without mutating
    # the caller's row_factory or committing its pending transaction.
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in cursor.execute(sql, tuple(args))]
    finally:
        cursor.close()


def _batches(ids: Iterable[int]) -> Iterable[list[int]]:
    values = sorted(set(ids))
    for start in range(0, len(values), 400):
        yield values[start:start + 400]


def _completed_warrant_reference(title: str) -> bool:
    """A closed exercise reference with one real series, not a new instrument."""
    if not re.search(
        r'^(?:den\s+)?avslutad(?:e)?\s+(?:utnyttjande|nyttjande)period(?:en)?\s+(?:av|för)\s+teckningsoptioner\b'
        r'|^(?:the\s+)?(?:completed|concluded|closed)\s+exercise(?:\s+period)?\s+(?:of|for)\s+(?:the\s+)?warrants\b',
        title, re.I,
    ):
        return False
    if any(_RX[name].search(title) for name in (
        'rights_issue', 'unit_issue', 'directed_issue', 'convertible',
        'bridge_loan', 'shareholder_loan', 'credit_facility',
    )):
        return False
    series = {re.sub(r'\s', '', match.group(1) or match.group()).upper()
              for match in _SERIES.finditer(title)}
    return len(series) == 1


def _underwriter_compensation_title(title: str, *, reviewed: bool = False) -> str | None:
    """Separate the present compensation issue from its referenced offer."""
    parts = re.split(r'\s+(?:in\s+connection\s+with|i\s+samband\s+med)\s+', title,
                     maxsplit=1, flags=re.I)
    recipient = r'underwrit|emissionsgarant|garantiersättning'
    if reviewed:
        # A plain guarantor recipient needs the audited document assignment.
        # Keep raw membership stable so an existing proposal's saved expected
        # state remains valid, and revert restores the original projection.
        recipient += (r'|\bto\s+(?:(?:a|certain|the)\s+)?guarantors?\b(?![\u0027\u2019])'
                      r'|\btill\s+garant(?:en|er(?:na)?)?\b')
    if (len(parts) == 2 and _RX['directed_issue'].search(parts[0])
            and re.search(recipient, parts[0], re.I)):
        if _RX['rights_issue'].search(parts[1]):
            return parts[0]
        # Only audited assignments can separate a current share issue from a
        # completed warrant period. A present warrant grant, mixed series or
        # unrelated simultaneous financing retains its original categories.
        if (reviewed and not _RX['warrant_exercise'].search(parts[0])
                and _completed_warrant_reference(parts[1])):
            return parts[0]
    return None


def _categories(title: str, *, reviewed: bool = False) -> list[str]:
    found = {key for key, rx in _RX.items() if rx.search(title)}
    if _underwriter_compensation_title(title, reviewed=reviewed) is not None:
        found.difference_update({'rights_issue', 'unit_issue'})
        reference = re.split(r'\s+(?:in\s+connection\s+with|i\s+samband\s+med)\s+',
                             title, maxsplit=1, flags=re.I)[-1]
        if reviewed and _completed_warrant_reference(reference):
            found.discard('warrant_exercise')
    if "rights_issue" in found:
        found.discard("unit_issue")
    if found & {"rights_issue", "directed_issue", "unit_issue", "convertible"}:
        found.discard("other_equity")
    if "convertible" in found:
        found.discard("directed_issue")
    if found & {"bridge_loan", "shareholder_loan", "credit_facility", "convertible"}:
        found.discard("other_debt")
    if "warrant_exercise" in found and found & {"rights_issue", "directed_issue", "unit_issue"}:
        if not re.search(r"utnyttj|nyttjande|inlösen|exercise|subscription.*warrants", title, re.I):
            found.discard("warrant_exercise")
    return sorted(found)


def _status(title: str) -> str:
    # A negative or explicitly future statement must not advance the lifecycle.
    # Examples: "has not yet been registered" and "subscription begins next week".
    if re.search(
        r"(?:ännu|fortfarande)\s+inte\s+(?:registrer|fullteck)|"
        r"(?:not|hasn.t|have not)\s+(?:yet\s+)?(?:been\s+)?(?:registered|fully subscribed)",
        title,
        re.I,
    ):
        return "updated"
    tests = (
        # “Avbryter utvecklingen ... genomför en emission” is not a cancelled
        # emission. The cancellation verb must grammatically target the offer.
        ("cancelled", r"ställ\w*\s+in\s+(?:den\s+)?(?:planerade\s+)?(?:företrädes)?emission|"
                      r"avbryt\w*\s+(?:den\s+)?(?:planerade\s+)?(?:företrädes)?emission|"
                      r"cancel\w*\s+(?:the\s+)?(?:rights\s+)?(?:share\s+)?issue|withdraw\w*\s+(?:the\s+)?offer"),
        ("preliminary_outcome", r"preliminär\w*.*utfall|preliminary.*(?:outcome|result)"),
        ("registered", r"registrer\w*|registered|registration"),
        ("completed", r"utfall|outcome|överteck\w*|fullteck\w*|slutför\w*|har\s+genomfört|"
         r"completed?|oversubscri\w*|result\w*\s+of\s+(?:the\s+)?(?:exercise|subscription|rights)|fully\s+subscribed"),
        ("proposed", r"föreslår|förslag\w*|propos\w*|avser|intends?|explores?|utreder\s+förutsättning"),
        ("conditional", r"villkorad\w*|conditional|subject\s+to"),
        (
            "subscription_period",
            r"(?:teckningsperiod|utnyttjandeperiod).*(?:inled|pågår|öppn)|"
            r"(?:inled|pågår|öppn).*(?:teckningsperiod|utnyttjandeperiod)|"
            r"(?:subscription|exercise)\s+period.*(?:has\s+commenced|is\s+open|begins?\s+today)|"
            r"(?:has\s+commenced|is\s+open|begins?\s+today).*(?:subscription|exercise)\s+period",
        ),
        ("approved", r"beslut\w*|resolv\w*|approv\w*|godkänn\w*"),
        ("updated", r"uppdater\w*|update\w*|påminn\w*|reminder|teckningsperiod|subscription\s+period|prospekt|prospectus"),
    )
    return next((status for status, pattern in tests if re.search(pattern, title, re.I)), "announced")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+", _clean(text)) if part.strip()]


def _transaction_status(title: str, body: str, category: str, *, reviewed: bool = False) -> str:
    primary_title = _underwriter_compensation_title(title, reviewed=reviewed)
    status = _status(primary_title if primary_title is not None else title)
    if category != "rights_issue":
        return status
    # A proposal concerning an optional overallotment does not undo the
    # board's decision on the principal rights issue.
    parts = re.split(r"\s+(?:samt|och|and)\s+(?:föreslår|proposes?)\b", title, maxsplit=1, flags=re.I)
    if (len(parts) == 2 and re.search(r"övertilldel|over.allotment", parts[1], re.I)
            and _status(parts[0]) == "approved"):
        return "approved"
    # Some result headlines state only proceeds received. Require both that
    # headline and an explicit final-result statement in the current opening.
    if (status == "announced" and re.search(r"tillförs|receives?|raises?", title, re.I)
            and re.search(r"har\s+erhållit\s+(?:det\s+)?slut(?:giltiga|liga)\s+utfallet|"
                          r"has\s+received\s+(?:the\s+)?final\s+(?:outcome|result)", body, re.I)):
        return "completed"
    return status


def _numeric(text: str) -> float:
    value = re.sub(r"\s", "", text)
    if "," in value and "." in value:
        decimal = max(value.rfind(","), value.rfind("."))
        value = re.sub(r"[.,]", "", value[:decimal]) + "." + value[decimal + 1:]
    elif re.fullmatch(r"\d{1,3}(?:[, .]\d{3})+", value):
        value = re.sub(r"[,.]", "", value)
    else:
        value = value.replace(",", ".")
    return float(value)


def _money(text: str) -> list[tuple[re.Match, float, str]]:
    values = []
    for match in _MONEY.finditer(text):
        unit = (match["unit"] or match["currency"]).upper()
        scale = (match["scale"] or unit).casefold()
        multiplier = (1e9 if re.search(r"billion|miljard", scale) else
                      1e6 if re.search(r"million|miljon|^m(?:sek|eur|usd|nok|dkk|gbp)$", scale) else
                      1e3 if re.search(r"thousand|tusen|^[tk]sek$", scale) else 1)
        currency = next((c for c in ("SEK", "EUR", "USD", "NOK", "DKK", "GBP") if c in unit), "SEK")
        try:
            amount = _numeric(match["prefix"] or match["suffix"]) * multiplier
        except ValueError:
            continue
        if math.isfinite(amount) and amount >= 0:
            values.append((match, amount, currency))
    return values


@dataclass
class _Candidate:
    doc: dict
    category: str
    status: str
    body: str
    events: list[dict]
    series: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    amount: float | None = None
    currency: str | None = None
    basis: str | None = None
    review: bool = False

    @property
    def text(self) -> str:
        return self.doc["title"] + ". " + self.body

    @property
    def time(self) -> datetime:
        return _stamp(self.doc["published_at"])


def _identity(candidate: _Candidate) -> None:
    category = candidate.category
    for sentence in _sentences(candidate.text):
        if not _RX[category].search(sentence):
            continue
        # Do not treat the date of a bridge loan as a rights-issue identity.
        if len(_categories(sentence)) != 1:
            continue
        if category in {"warrant_exercise", "convertible"}:
            candidate.series.update(re.sub(r"\s", "", m.group(1) or m.group()).upper()
                                    for m in _SERIES.finditer(sentence))
        if _IDENTITY_CONTEXT.search(sentence):
            for match in _DATE.finditer(sentence):
                value = match[1] or f"{match[4]}-{_MONTHS[match[3].casefold()]:02d}-{int(match[2]):02d}"
                try:
                    datetime.fromisoformat(value)
                except ValueError:
                    continue
                candidate.dates.add(value)
    candidate.urls.update(re.findall(r"https?://[^\s<>\"()]+", candidate.body))


def _amount(candidate: _Candidate) -> None:
    if candidate.status == "cancelled":
        return
    completed = candidate.status in {"completed", "registered", "preliminary_outcome"}
    title_categories = _categories(candidate.doc["title"])
    if candidate.category == "credit_facility":
        for sentence in _sentences(candidate.text):
            if not re.search(
                r"(?:höjs|utökas)\s+från.+?\btill\b|(?:increased|extended)\s+from.+?\bto\b",
                sentence, re.I,
            ):
                continue
            values = _money(sentence)
            if len(values) >= 2:
                _, amount, currency = values[-1]
                candidate.amount, candidate.currency, candidate.basis = (
                    amount, currency, "credit_limit",
                )
                return
    # Prefer explicit title terms. Otherwise require an unambiguous source
    # sentence about this financing, not an arbitrary number in the document.
    for text in (candidate.doc["title"], *_sentences(candidate.body)):
        categories = _categories(text)
        if candidate.category not in categories:
            if categories or len(title_categories) != 1 or not re.search(
                r"bolaget\s+tillförs|company\s+(?:will\s+)?receives|gross\s+proceeds|net\s+proceeds", text, re.I
            ):
                continue
        if re.search(r"tidigare|historisk|previous|last\s+year|föregående|per\s+(?:aktie|unit|share)|"
                     r"(?:under|during|in)\s+20\d{2}", text, re.I):
            continue
        if completed and re.search(r"vid\s+full|full\s+subscription|kan\s+tillföra|could\s+raise|"
                                   r"högst|upp\s+till|up\s+to|maximum", text, re.I):
            continue
        # Outcome text must say what was raised, not repeat the announced size.
        if completed and text != candidate.doc["title"] and not re.search(
            r"tillförs|tillfördes|inbringade|utfall|outcome|raised|receives|proceeds|subscribed", text, re.I
        ):
            continue
        choices = []
        for match, value, currency in _money(text):
            # MFN page captures can repeat a financing headline in the page
            # chrome, then mention unrelated loans much later in the same
            # extracted sentence.  A body amount must be close to a term for
            # this transaction; a title elsewhere on the page is not enough.
            if text != candidate.doc["title"]:
                vicinity = text[max(0, match.start() - 180):match.end() + 180]
                explicit_proceeds = re.search(
                    r"bolaget\s+tillförs|company\s+(?:will\s+)?receives|"
                    r"gross\s+proceeds|net\s+proceeds|inbringade|raised",
                    vicinity,
                    re.I,
                )
                if not _RX[candidate.category].search(vicinity) and not explicit_proceeds:
                    continue
            before = text[:match.start()]
            mentions = [(m.start(), key) for key, rx in _RX.items() for m in rx.finditer(before)
                        if key in categories]
            if mentions and max(mentions)[1] != candidate.category:
                continue
            local = before[max(0, max((pos for pos, _ in mentions), default=0)):]
            if re.search(r"garanti|guarantee|åtagande|undertaking|kostnad|cost|övertilldelning|"
                         r"overallotment|ersättning|compensation|återbetal|repayment", local, re.I):
                continue
            basis = "outcome_amount" if completed else "announced_amount"
            vicinity = text[max(0, match.start() - 45):match.end() + 65]
            if re.search(r"netto|net\s+proceeds|efter\s+(?:avdrag\s+för\s+)?(?:emissions|transaktions)?kostnader", vicinity, re.I):
                basis = "outcome_net" if completed else "announced_net"
            elif re.search(r"brutto|gross|före\s+(?:emissions|transaktions)?kostnader", vicinity, re.I):
                basis = "outcome_gross" if completed else "announced_gross"
            elif re.search(r"upp\s+till|up\s+to|högst|maximum|vid\s+full", local, re.I):
                basis = "maximum_proceeds"
            elif candidate.category in {"bridge_loan", "shareholder_loan", "other_debt"}:
                basis = "loan_principal"
            elif candidate.category == "credit_facility":
                basis = "credit_limit"
            choices.append((value, currency, basis))
        choices = list(dict.fromkeys(choices))
        if len(choices) == 1:
            candidate.amount, candidate.currency, candidate.basis = choices[0]
            return
        if choices:
            candidate.review = True
            return


def _language(candidate: _Candidate) -> str:
    title = candidate.doc["title"]
    swedish = bool(re.search(
        r"\b(?:företrädesemission|riktad\w*\s+emission|utfall|offentliggör|beslutar|teckningsoptioner|kreditfacilitetsavtal|kreditfacilitet|låneavtal|konvertib\w*)\b",
        title,
        re.I,
    ))
    english = bool(re.search(
        r"\b(?:rights issue|directed (?:share )?issue|private placement|outcome|announces?|resolves?|warrants?|credit facility|loan agreement|convertible\w*)\b",
        title,
        re.I,
    ))
    if swedish != english:
        return "sv" if swedish else "en"
    declared = (candidate.doc.get("language") or "").lower()
    if declared in {"sv", "en"}:
        return declared
    return "sv" if re.search(r"företräde|riktad|emission|utfall|beslut|brygg|tecknings|konvertib", title, re.I) else "en"


def _title_facts(candidate: _Candidate) -> tuple[set[str], set[str]]:
    title = candidate.doc["title"].casefold()
    title = title.replace(candidate.doc["issuer_name"].casefold(), " ")
    for rx in _RX.values():
        title = rx.sub(" financing ", title)
    replacements = (
        (r"offentliggör|announces?|redovisar|reports?|meddelar", "announce"),
        (r"beslutar|resolves?|beslutade", "resolve"),
        (r"utfall|outcome|resultat", "outcome"),
        (r"s[äa]kerhetsaktier|collateral\s+shares", "collateral"),
        (r"ersättningsaktier|compensation\s+shares", "compensation"),
        (r"slutför|completes?|genomför", "complete"),
    )
    for pattern, replacement in replacements:
        title = re.sub(r"\b(?:" + pattern + r")\b", replacement, title)
    numbers = {f"{amount:g}:{currency}" for _, amount, currency in _money(candidate.doc["title"])}
    title = _MONEY.sub(" amount ", title)
    stop = {"the", "a", "an", "of", "on", "in", "och", "and", "om", "en", "ett", "av", "i",
            "sin", "its", "to", "med", "with", "för", "for", "cirka", "approximately", "ab", "publ"}
    words = {w for w in re.findall(r"\w+", title) if w not in stop}
    return words, numbers


def _translations(left: _Candidate, right: _Candidate) -> bool:
    if abs((left.time - right.time).total_seconds()) > 300 or left.time == datetime.min.replace(tzinfo=timezone.utc):
        return False
    if _clean(left.doc["title"]).casefold() == _clean(right.doc["title"]).casefold():
        return True
    if _language(left) == _language(right):
        return False
    opening_statuses = {
        "proposed", "announced", "conditional", "approved", "subscription_period", "updated",
    }
    if left.status != right.status and not {left.status, right.status} <= opening_statuses:
        return False
    lw, ln = _title_facts(left)
    rw, rn = _title_facts(right)
    if left.series != right.series:
        return False
    # Matching monetary facts strengthen a title match, but separate tranches,
    # collateral and compensation issues must never collapse into one another.
    qualifiers = r"\b(?:TO\s*\d+|\d+|first|second|third|första|andra|tredje|collateral|compensation)\b"
    extras_l = set(re.findall(qualifiers, " ".join(sorted(lw)), re.I))
    extras_r = set(re.findall(qualifiers, " ".join(sorted(rw)), re.I))
    if ln == rn and (lw == rw or (bool(ln) and extras_l == extras_r and len(lw & rw) >= 2)):
        return True
    # MFN commonly publishes the Swedish and English editions a few seconds
    # apart.  Their wording need not share enough literal tokens to pass the
    # conservative title-fact comparison above.  A matching category, status,
    # publication window and monetary fact is still a sufficiently specific
    # translation identity, provided an instrument series does not contradict
    # it (checked above).  This happens before pagination, so one bilingual
    # event never consumes two rows on the first page.
    return (
        left.amount is not None
        and (left.amount, left.currency) == (right.amount, right.currency)
    )


def _same_stage_update(left: _Candidate, right: _Candidate) -> bool:
    """Link same-day releases that describe the same lifecycle stage.

    Issuers sometimes publish an outcome twice in the same language, for
    example a short regulatory release followed by a fuller adviser/agent
    version.  The pair should enrich one transaction dossier, while different
    tranches or monetary sizes must remain separate.
    """
    if left.status != right.status or left.time.date() != right.time.date():
        return False
    if left.series and right.series and left.series != right.series:
        return False
    left_words, left_numbers = _title_facts(left)
    right_words, right_numbers = _title_facts(right)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    if (left.amount is not None and right.amount is not None
            and (left.amount, left.currency) != (right.amount, right.currency)):
        return False
    # MFN translations are not always released inside the five-minute window
    # used by the strict translation matcher. On the same publication day,
    # different declared languages and the same lifecycle stage are enough
    # when series and monetary facts do not conflict (checked above).
    if _language(left) != _language(right):
        return True
    shared = left_words & right_words
    smaller = min(len(left_words), len(right_words))
    return smaller > 0 and len(shared) >= min(3, smaller) and len(shared) / smaller >= 0.6


def _signal(group: list[_Candidate], category: str, status: str,
            *, directed_cash_outcome: bool = False) -> tuple[str, str]:
    """Return one lifecycle-consistent visual signal for a transaction.

    A planned offering may mention maximum proceeds ``vid full teckning``. It
    is not an outcome, so only closing-stage releases can produce a green or
    red subscription signal.  Directed issues are green once announced because
    they do not have a public subscription outcome to await.
    """
    opening = {"proposed", "announced", "conditional", "approved", "subscription_period"}
    closing = {"preliminary_outcome", "completed", "registered"}
    equity_categories = {"rights_issue", "unit_issue", "other_equity", "warrant_exercise"}

    if status == "cancelled":
        return "red", "Inställd emission"
    if category == "directed_issue" and (directed_cash_outcome or status in closing):
        return "green", "Riktad emission"
    if category == "directed_issue" and status in opening:
        return "yellow", "Planerad riktad emission"
    if category in equity_categories and status in opening:
        return "yellow", "Planerad emission"
    if category not in equity_categories or status not in closing:
        return "neutral", "Utfall eller villkor utan teckningsgrad"

    # Restrict the textual result to releases which actually close the deal.
    # Earlier announcements must never override the latest lifecycle stage.
    outcome_text = " ".join(candidate.text for candidate in group if candidate.status in closing)
    if re.search(
        r"underteck\w*|inte\s+fullteck\w*|delvis\s+teck\w*|"
        r"undersubscri\w*|not\s+fully\s+subscribed|partly\s+subscribed",
        outcome_text,
        re.I,
    ):
        return "red", "Undertecknad emission"
    if re.search(r"fullteck\w*|överteck\w*|fully\s+subscribed|oversubscri\w*", outcome_text, re.I):
        return "green", "Fulltecknad emission"
    return "neutral", "Utfall eller villkor utan teckningsgrad"


def _title_tranche(candidate: _Candidate) -> int | None:
    """Identify the current tranche from the headline, never historical body mentions."""
    ordinals = {"first": 1, "första": 1, "second": 2, "andra": 2,
                "third": 3, "tredje": 3, "fourth": 4, "fjärde": 4,
                "fifth": 5, "femte": 5, "sixth": 6, "sjätte": 6,
                "seventh": 7, "sjunde": 7, "eighth": 8, "åttonde": 8,
                "ninth": 9, "nionde": 9, "tenth": 10, "tionde": 10}
    title = candidate.doc["title"].casefold()
    match = re.search(r"\b(" + "|".join(ordinals) + r")\s+tranche(?:n)?\b", title)
    if match:
        return ordinals[match[1]]
    match = re.search(r"\btranche(?:n)?\s*(?:nr\.?\s*|number\s*|#\s*)?(\d+)\b", title)
    return int(match[1]) if match else None


def _tranche_translation(left: _Candidate, right: _Candidate) -> bool:
    if not _translations(left, right):
        return False
    # Same amount/time/ordinal alone cannot link two different facilities.
    # Require a shared distinguishing headline anchor such as the financier.
    left_words, _ = _title_facts(left)
    right_words, _ = _title_facts(right)
    generic = {"financing", "tranche", "tranchen", "first", "första", "second", "andra",
               "third", "tredje", "fourth", "fjärde", "fifth", "femte", "sixth", "sjätte",
               "seventh", "sjunde", "eighth", "åttonde", "ninth", "nionde", "tenth", "tionde",
               "request", "requests", "begär", "drawdown", "utbetalning", "under",
               "announce", "resolve", "complete", "outcome", "group", "grupp"}
    issuer_words = set(re.findall(r"\w+", str(left.doc.get("issuer_name", "")).casefold()))
    anchors = (left_words & right_words) - generic - issuer_words
    return any(len(word) >= 3 and word.isalpha() for word in anchors)


def _related(left: _Candidate, right: _Candidate) -> bool:
    # A source whose neutral premise changed must be visible for fresh review;
    # its stale reviewed key cannot bridge it back into the bound finance card.
    if bool(left.doc.get('_binding_source_changed')) != bool(right.doc.get('_binding_source_changed')):
        return False
    left_forced = str(left.doc.get("effective_round_key") or "").strip().casefold()
    right_forced = str(right.doc.get("effective_round_key") or "").strip().casefold()
    left_link = left.doc.get("linked_transaction_id")
    right_link = right.doc.get("linked_transaction_id")
    if left_link and right_link and left_link != right_link:
        return False
    # An audited document overlay is stronger than every automatic heuristic.
    # Two corrected groups must never be rejoined by amount, stage or title.
    if left_forced and right_forced:
        return left_forced == right_forced
    if left.series and right.series and left.series != right.series:
        return False
    if left_forced or right_forced:
        forced = left_forced or right_forced
        other = right if left_forced else left
        other_rounds = {
            str(event.get("canonical_round_key") or "").strip().casefold()
            for event in other.events if event.get("canonical_round_key")
        }
        return forced in other_rounds and abs((left.time - right.time).days) <= 730
    left_tranche, right_tranche = _title_tranche(left), _title_tranche(right)
    if left_tranche is not None or right_tranche is not None:
        if left_tranche != right_tranche:
            # A facility and its drawdowns, or two distinct drawdowns, are not
            # one instrument even if extraction reused the facility round key.
            return False
        if _tranche_translation(left, right):
            # Raw model-generated keys can differ across languages. A narrow
            # publication/ordinal/financier match beats those raw keys only;
            # audited assignments above remain authoritative.
            return True
    left_rounds = {
        str(event.get("canonical_round_key") or "").strip().casefold()
        for event in left.events if event.get("canonical_round_key")
    }
    right_rounds = {
        str(event.get("canonical_round_key") or "").strip().casefold()
        for event in right.events if event.get("canonical_round_key")
    }
    # A source-backed round key is the strongest lifecycle identity available.
    # Keep a generous time bound for registrations that trail the offer, while
    # still preventing an accidentally reused key from joining distant rounds.
    if left_rounds & right_rounds:
        return abs((left.time - right.time).days) <= 730
    if left_rounds and right_rounds:
        return False
    if _translations(left, right):
        return True
    if _same_stage_update(left, right):
        return True
    # A rights issue is commonly published as proposal, terms, preliminary
    # outcome and final outcome over several weeks.  When the releases name
    # the same financing category and do not carry conflicting amounts, keep
    # the lifecycle together even if an older extraction has no round key.
    # This is intentionally limited to rights issues and to different stages:
    # two same-day placements remain separate transactions.
    if left.category == right.category == "rights_issue" and left.status != right.status:
        age = abs((left.time - right.time).days)
        same_currency = not (left.currency and right.currency) or left.currency == right.currency
        compatible_amount = (
            left.amount is None or right.amount is None
            or max(left.amount, right.amount) <= min(left.amount, right.amount) * 1.25
        )
        if age <= 180 and same_currency and compatible_amount:
            return True
    # An accelerated bookbuild may be upsized between launch and pricing.
    # Require the pricing release to explicitly close the bookbuild announced
    # earlier that same day; amount proximity alone is not round identity.
    earlier, later = (left, right) if left.time < right.time else (right, left)
    if (earlier.category == "directed_issue" and earlier.status == "proposed"
            and earlier.time.date() == later.time.date() and earlier.time < later.time
            and re.search(r"bookbuild", earlier.body, re.I)
            and re.search(
                r"bookbuilding[^.!?]{0,160}(?:tidigare\s+idag|earlier\s+today)"
                r"[^.!?]{0,100}(?:slutförts|completed|concluded)", later.body, re.I)):
        return True
    # Backward-compatible lifecycle identity for events extracted before
    # canonical_round_key existed. Successive stages with the same approximate
    # deal size are one round even when each release carries new timetable
    # dates. Requiring different stages and a tight amount tolerance keeps two
    # contemporaneous raises separate.
    opening_rank = {
        "proposed": 0, "announced": 1, "conditional": 2,
        "approved": 3, "subscription_period": 4,
    }
    if left.status in opening_rank and right.status in opening_rank and left.status != right.status:
        earlier, later = (left, right) if left.time <= right.time else (right, left)
        age = (later.time - earlier.time).days
        same_currency = not (left.currency and right.currency) or left.currency == right.currency
        compatible_amount = (
            left.amount is not None and right.amount is not None
            and max(left.amount, right.amount) <= min(left.amount, right.amount) * 1.10
        )
        if 0 <= age <= 180 and same_currency and compatible_amount:
            return True
    # A URL is an identity only if it identifies another stored release.
    # Timetable dates change between terms and outcome. An explicit link to
    # the stored release survives that change; conflicting round keys and
    # instrument series have already been rejected above.
    if left.doc["source_url"] in right.urls or right.doc["source_url"] in left.urls:
        return True
    if left.dates and right.dates and not left.dates & right.dates:
        return False
    if left.series and left.series == right.series:
        # Series names can be reused across years; a bounded instrument life
        # avoids joining successive uses of a bare TO1 indefinitely.
        return abs((left.time - right.time).days) <= 550
    if left.dates & right.dates:
        # A board can decide two separate placements on the same day. A date
        # alone cannot resolve conflicting announced sizes; retain both rows.
        opening = {"announced", "approved", "proposed", "conditional"}
        if (left.status in opening and right.status in opening
                and left.amount is not None and right.amount is not None
                and (left.amount, left.currency) != (right.amount, right.currency)):
            return False
        return True
    # An outcome may explicitly refer to the announcement's publication date.
    for earlier, later in ((left, right), (right, left)):
        if (earlier.status in {"announced", "approved", "proposed", "conditional"}
                and later.time > earlier.time
                and earlier.doc["published_at"][:10] in later.dates):
            return True
    # Lifecycle fallback: an outcome often omits the original decision date and
    # URL. Link it to a recent opening stage when currency/series do not
    # conflict. If two open rounds are plausible, the grouping loop sees two
    # matches and deliberately keeps the outcome separate for review.
    opening = {"proposed", "announced", "conditional", "approved", "subscription_period"}
    closing = {"preliminary_outcome", "completed", "registered", "cancelled"}
    for earlier, later in ((left, right), (right, left)):
        age = (later.time - earlier.time).days
        if earlier.status in opening and later.status in closing and 0 <= age <= 180:
            if (earlier.currency and later.currency
                    and earlier.currency != later.currency):
                return False
            if (earlier.amount is not None and later.amount is not None
                    and later.amount > earlier.amount * 1.25):
                return False
            return True
    return False


def _institutions(group: list[_Candidate], participants: dict[int, list[dict]]) -> list[dict]:
    result: dict[tuple[str, str], dict] = {}

    def add(name: str, role: str) -> None:
        name = _clean(name).strip(" ,:–-")
        if name and len(name.split()) <= 9:
            result[(name.casefold(), role)] = {"name": name, "role": role}

    for candidate in group:
        for sentence in _sentences(candidate.body):
            # Necessary phrase guard: avoid name-regex backtracking in unrelated prose.
            if not _ROLE_HINT.search(sentence):
                continue
            # Explicit role wording, not a Certified Adviser footer or a name
            # merely appearing somewhere in the release.
            for role, pattern in _ROLE_WORDS.items():
                if not _ROLE_HINTS[role].search(sentence):
                    continue
                name = r"(?P<name>[A-ZÅÄÖ][\wÅÄÖåäö&.-]*(?:\s+(?:[A-ZÅÄÖ][\wÅÄÖåäö&.-]*|&)){0,7})"
                for match in re.finditer(name + r"\s+(?i:är|agerar\s+som|is|acts?\s+as)\s+(?i:" + pattern + r")\b", sentence):
                    add(match["name"], role)
                for match in re.finditer(r"(?i:" + pattern + r")\s+(?i:är|is|:)\s*" + name, sentence):
                    add(match["name"], role)
            # Common MFN wording lists several advisers between "engaged" and
            # the role. Keep this source-bound and split only legal company
            # names; free-form footer names remain excluded.
            for match in re.finditer(
                r"(?i:(?:has\s+)?engaged|har\s+anlitat)\s+(?P<names>.+?)\s+"
                r"(?i:as|som)\s+(?i:financial\s+advis[eo]rs?|finansiell(?:a)?\s+rådgivare)\b",
                sentence,
            ):
                names = re.sub(r"\s*\((?:publ|publ\.)\)\s*", " ", match["names"], flags=re.I)
                for value in re.split(r"\s*,\s*|\s+(?:and|och)\s+", names):
                    if _INSTITUTION.search(value) or re.search(r"\b(?:AB|AS|ASA|A/S|Ltd|PLC)\b", value, re.I):
                        add(value, "Finansiell rådgivare")
            for match in (re.finditer(
                r"(?P<names>[A-ZÅÄÖ].+?)\s+(?i:have\s+entered\s+into\s+underwriting\s+commitments|"
                r"har\s+ingått\s+garantiåtaganden)\b",
                sentence,
            ) if _UNDERWRITING_HINT.search(sentence) else ()):
                names = re.sub(r"\s*\((?:publ|publ\.)\)\s*", " ", match["names"], flags=re.I)
                for value in re.split(r"\s*,\s*|\s+(?:and|och)\s+", names):
                    value = re.sub(r"^.*?\b(?:Issue|Emission)\W+", "", value, flags=re.I)
                    if _INSTITUTION.search(value) or re.search(r"\b(?:AB|AS|ASA|A/S|Ltd|PLC)\b", value, re.I):
                        add(value, "Garant")
        for event in candidate.events:
            for participant in participants.get(event["id"], []):
                role = _ROLE_MAP.get(participant.get("role"))
                source = (participant.get("source") or "").strip()
                if (role and participant.get("resolution_status") != "rejected"
                        and source in {candidate.doc["source_url"], candidate.doc["canonical_url"]}
                        and _clean(participant["raw_name"]).casefold() in candidate.text.casefold()):
                    add(participant["raw_name"], role)
    return sorted(result.values(), key=lambda row: (row["name"].casefold(), row["role"]))


def _percentage_from_text(text: str) -> float | None:
    match = re.search(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:%|procent|percent)\b", text, re.I)
    if not match:
        return None
    try:
        return _numeric(match.group(1)) / 100
    except ValueError:
        return None


def _guarantee_fee_from_text(group: list[_Candidate]) -> tuple[float | None, float | None]:
    """Extract an explicitly stated guarantee fee amount/rate from source text.

    This presentation helper never persists or alters screening data. It only
    accepts a money value from a sentence that also identifies compensation,
    avoiding the guaranteed amount itself.
    """
    amount = None
    rate = None
    fee_context = re.compile(
        r"garantiersättning|garantiavgift|ersättning\s+(?:till\s+)?garant|"
        r"underwriting\s+fee|guarantee\s+fee|fee\s+(?:payable\s+)?to\s+(?:the\s+)?underwriter",
        re.I,
    )
    other_measure = re.compile(
        r"aktiekapital|share\s+capital|teckningskurs|subscription\s+price|"
        r"garanterat\s+belopp|underwritten\s+amount|antal\w*\s+aktier|number\s+of\s+shares",
        re.I,
    )
    for candidate in sorted(group, key=lambda value: (value.time, value.doc["id"]), reverse=True):
        for sentence in _sentences(candidate.text):
            marker = fee_context.search(sentence)
            if marker is None:
                continue
            # Compensation paid in shares is often followed by the resulting
            # share capital. Those accounting amounts are not the fee itself.
            # Read only the fee clause up to the next explicitly named metric.
            clause = sentence[marker.end():]
            boundary = other_measure.search(clause)
            if boundary:
                clause = clause[:boundary.start()]
            if rate is None:
                rate = _percentage_from_text(clause)
            if amount is None:
                values = _money(clause)
                # Multiple sums in a clause need explicit structured evidence;
                # never select an arbitrary terminal balance as compensation.
                if len(values) == 1:
                    amount = values[0][1]
            if amount is not None and rate is not None:
                return amount, rate
    return amount, rate


def _source_financing_terms(group: list[_Candidate]) -> dict[str, Any]:
    """Read explicit legacy ECM terms from every release in a linked round.

    Historical extraction rows can pre-date the structured financing fields.
    These conservative presentation fallbacks expose only values stated in the
    linked source text and do not write to the database or affect screening.
    """
    terms: dict[str, Any] = {}
    ordered = sorted(group, key=lambda value: (value.time, value.doc["id"]), reverse=True)

    def set_percent(key: str, sentence: str, context: str) -> None:
        if key in terms:
            return
        context_match = re.search(context, sentence, re.I)
        if context_match:
            window = sentence[max(0, context_match.start() - 120):context_match.start()]
            matches = list(re.finditer(
                rf"(?<![\d.,])({_NUMBER})\s*(?:%|procent|percent)\b",
                window,
                re.I,
            ))
            match = matches[-1] if matches else None
        else:
            match = None
        if match is not None:
            try:
                terms[key] = _numeric(match.group(1)) / 100
            except ValueError:
                pass

    for candidate in ordered:
        for sentence in _sentences(candidate.text):
            set_percent(
                "subscription_with_rights_pct", sentence,
                r"(?:med\s+stöd\s+av|by\s+(?:the\s+)?exercise\s+of|"
                r"with\s+(?:the\s+)?support\s+of)\s+(?:unit|subscription)?\s*rights?",
            )
            set_percent(
                "subscription_without_rights_pct", sentence,
                r"(?:utan\s+stöd\s+av|without\s+(?:the\s+)?(?:exercise|support)\s+of)"
                r"\s+(?:unit|subscription)?\s*rights?",
            )
            set_percent(
                "guarantee_take_up_pct_of_issue", sentence,
                r"(?:teckn\w*\s+av\s+garant|subscribed\s+for\s+by\s+(?:the\s+)?underwriter)",
            )
            if (candidate.status in {"preliminary_outcome", "completed", "registered"}
                    and "final_subscription_pct" not in terms and re.search(
                r"(?:fullteckn\w*|tecknades\s+till\s+100\s*(?:%|procent)|"
                r"subscribed\s+for\s+in\s+full|fully\s+subscribed)",
                sentence,
                re.I,
            )):
                terms["final_subscription_pct"] = 1.0

            cost_match = re.search(
                r"(?:emissions|transaktions)kostnader?|"
                r"(?:issue(?:-related)?|transaction)\s+costs?",
                sentence,
                re.I,
            )
            if "transaction_cost" not in terms and cost_match:
                values = [value for value in _money(sentence) if value[0].start() > cost_match.start()]
                if values:
                    # In "costs 4m excluding underwriting fees of 3m", the
                    # first amount is the issue cost and the latter the fee.
                    terms["transaction_cost"] = values[0][1]

            if "issue_price" not in terms and re.search(
                r"(?:teckningskurs|subscription\s+price)", sentence, re.I,
            ) and not re.search(r"(?:teckningsoption|warrant)", sentence, re.I):
                values = _money(sentence)
                if values:
                    terms["issue_price"] = values[0][1]

        # Exact commitment tables are more precise than rounded prose.  The
        # table is accepted only when both commitment headers and a Total row
        # are present in the same source document.
        text = candidate.text
        if (re.search(r"subscription commitment\s*\(SEK\)", text, re.I)
                and re.search(r"guarantee commitment\s*\(SEK\)", text, re.I)):
            total = re.search(
                r"\bTotal(?=\d|\s|\|)\s*\|?\s*"
                r"(?P<subscription>\d{1,3}(?:[\s,]\d{3})+)\s*\|?\s*"
                r"(?P<subscription_pct>\d{1,3}(?:[.,]\d+?)?)\s*\|?\s*"
                r"(?P<guarantee>\d{1,3}(?:[\s,]\d{3})+)\s*\|?\s*"
                r"(?P<guarantee_pct>\d{1,3}(?:[.,]\d+)?)\b",
                text,
                re.I,
            )
            if total:
                terms["subscription_commitment_amount"] = _numeric(total["subscription"])
                terms["subscription_commitment_pct"] = _numeric(total["subscription_pct"]) / 100
                terms["guarantee_commitment_amount"] = _numeric(total["guarantee"])
                terms["guarantee_commitment_pct"] = _numeric(total["guarantee_pct"]) / 100

        allotted = re.search(
            r"(?P<shares>\d{1,3}(?:[\s,]\d{3})+)\s+shares?[^.!?]{0,100}"
            r"(?:subscribed\s+for\s+and\s+allotted|allotted)[^.!?]{0,100}"
            r"corresponding\s+to\s+(?:approximately\s+)?"
            r"(?P<pct>\d+(?:[.,]\d+)?)\s*(?:%|percent)",
            text, re.I,
        ) or re.search(
            r"(?:In\s+total,?\s*)"
            r"(?P<shares>\d{1,3}(?:[\s,]\d{3})+)\s+shares?\s*,?\s*"
            r"corresponding\s+to\s+(?:approximately\s+)?"
            r"(?P<pct>\d+(?:[.,]\d+)?)\s*(?:%|percent)"
            r"[^.!?]{0,120}(?:have\s+been\s+)?allotted",
            text, re.I,
        )
        if allotted:
            terms["shares_issued"] = _numeric(allotted["shares"])
            terms["final_subscription_pct"] = _numeric(allotted["pct"]) / 100

    with_rights = terms.get("subscription_with_rights_pct")
    without_rights = terms.get("subscription_without_rights_pct")
    if with_rights is not None and without_rights is not None:
        terms["subscription_without_guarantees_pct"] = with_rights + without_rights
    return terms


def _financing_dossier(group: list[_Candidate]) -> dict[str, Any]:
    """Create a source-bound ECM dossier without changing stored calculations."""
    event_by_id: dict[int, dict] = {}
    for candidate in group:
        for event in candidate.events:
            event_by_id[int(event["id"])] = event
    stage_order = {
        "considering": 0, "announced": 1, "terms_published": 2,
        "subscription_open": 3, "preliminary_outcome": 4,
        "outcome_published": 5, "registered": 6, "settled": 7,
        "cancelled": 8,
    }
    events = sorted(event_by_id.values(), key=lambda event: (
        stage_order.get(str(event.get("lifecycle_stage") or ""), -1),
        str(event.get("stage_effective_at") or event.get("effective_date") or
            event.get("announcement_date") or ""),
        int(event["id"]),
    ))

    def latest(field: str) -> Any:
        return next((event.get(field) for event in reversed(events)
                     if event.get(field) is not None), None)

    source_terms = _source_financing_terms(group)
    fee_details = latest("guarantee_fee_details")
    fee_details_document = None
    if fee_details is None:
        # Remuneration releases concern a set-off issue, not new proceeds in
        # the rights issue. Surface their explicit fee clause without importing
        # the set-off event's gross amount or other financing calculations.
        for candidate in sorted(group, key=lambda value: (value.time, _language(value) == "sv"), reverse=True):
            clauses = [sentence for sentence in _sentences(candidate.body)
                       if re.search(r"garantiersätt|garantiarvod|guarantee\s+(?:remuneration|compensation|fee)", sentence, re.I)
                       and re.search(r"procent|percent|kontant|cash|aktier|shares", sentence, re.I)]
            if clauses:
                fee_details = " ".join(clauses[:2])
                fee_details_document = int(candidate.doc["id"])
                break
    gross_amount = latest("gross_amount")
    if gross_amount is None:
        gross_amount = next((
            candidate.amount for candidate in sorted(
                group, key=lambda value: (value.time, value.doc["id"]), reverse=True
            )
            if candidate.amount is not None
            and candidate.basis in {"announced_gross", "outcome_gross"}
        ), None)
    explicit_net = latest("net_amount")
    transaction_cost = latest("transaction_cost")
    if transaction_cost is None:
        transaction_cost = source_terms.get("transaction_cost")
    transaction_cost_pct = latest("transaction_cost_pct")
    parsed_fee_amount, parsed_fee_pct = _guarantee_fee_from_text(group)
    fee_amount = latest("guarantee_fee_amount")
    fee_pct = latest("guarantee_fee_pct")
    if fee_amount is None:
        fee_amount = parsed_fee_amount
    if fee_pct is None:
        fee_pct = parsed_fee_pct
    combined_text = " ".join(candidate.text for candidate in group)
    cost_excludes_fee = bool(re.search(
        r"(?:(?:emissions|transaktions)kostnader?|(?:issue|transaction)\s+costs?).{0,80}"
        r"(?:exkl(?:usive)?|exclud(?:ing|es)?).{0,40}"
        r"(?:garanti|underwriting)|(?:exkl|exclud|excluding).{0,40}(?:garanti|underwriting).{0,80}cost",
        combined_text,
        re.I,
    ))
    total_cost = transaction_cost
    cost_basis = "source_reported" if transaction_cost is not None else None
    if transaction_cost is not None and cost_excludes_fee:
        cost_basis = "source_reported_excluding_guarantee_fee"
        if fee_amount is not None:
            total_cost = transaction_cost + fee_amount
    net_amount = explicit_net
    net_basis = "source_reported" if explicit_net is not None else None
    if net_amount is None and gross_amount is not None and total_cost is not None:
        net_amount = max(0.0, float(gross_amount) - float(total_cost))
        net_basis = "derived_gross_less_source_costs"

    def stored_or_source(field: str) -> Any:
        stored = latest(field)
        return source_terms.get(field) if stored is None else stored

    round_keys = sorted({
        str(event.get("canonical_round_key")).strip()
        for event in events if event.get("canonical_round_key")
    } | {
        str(candidate.doc["effective_round_key"]).strip()
        for candidate in group if candidate.doc.get("effective_round_key")
    })
    source_round_keys = list(round_keys)
    normalized_round_key = None
    if len(round_keys) == 1:
        normalized_round_key = round_keys[0]
    elif round_keys:
        # Once documents have already been reconciled into one transaction,
        # lifecycle-specific AI spellings are aliases in the effective read
        # model. Preserve every source key for audit, but expose one stable key
        # so the round is counted once downstream.
        normalized_round_key = min(
            round_keys,
            key=lambda key: (
                bool(re.search(r"(?:^|_)\d{1,2}(?:_|$)", key)),
                0 if "foretradesemission" in key.casefold() else 1,
                len(key), key,
            ),
        )
        round_keys = [normalized_round_key]
    # Keep the values used in a round linked to the document(s) that supplied
    # them.  The flat fields below are retained for existing consumers, while
    # this reconciliation object gives the editorial layer one consistent,
    # source-bound view of terms, actual outcome and costs.  It is presentation
    # data only: no value is persisted or fed into runway/screening logic.
    event_documents: dict[int, set[int]] = defaultdict(set)
    for candidate in group:
        for event in candidate.events:
            event_documents[int(event["id"])].add(int(candidate.doc["id"]))

    def field_documents(field: str, scope: list[dict] | None = None) -> list[int]:
        return sorted({
            document_id
            for event in (events if scope is None else scope) if event.get(field) is not None
            for document_id in event_documents.get(int(event["id"]), set())
        })

    def source_value(field: str, value: Any, *, derived: bool = False,
                     fallback: bool = False, scope: list[dict] | None = None) -> dict[str, Any]:
        return {
            "value": value,
            "source_document_ids": (
                sorted({int(candidate.doc["id"]) for candidate in group})
                if fallback else field_documents(field, scope)
            ),
            "basis": (
                "derived_from_source_values" if derived else
                "source_text_fallback" if fallback else "structured_source"
            ),
        }

    # Guarantee tiers are stored on the lifecycle event that disclosed them.
    # Project them to the round without inventing a split of an untiered total.
    # Where terms and outcome releases disclose different facts for the same
    # tier, each chosen field retains its own document provenance.
    tranches_by_tier: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for event in events:
        for tranche in event.get("guarantee_tranches") or []:
            tier = str(tranche.get("tier") or "").strip().casefold()
            if tier in {"top", "middle", "bottom", "other"}:
                tranches_by_tier[tier].append((event, tranche))

    def tranche_documents(event: dict, tranche: dict, field: str) -> list[int]:
        if tranche.get(field) is None:
            return []
        source_document_id = tranche.get("source_document_id")
        if source_document_id is not None:
            return [int(source_document_id)]
        return sorted(event_documents.get(int(event["id"]), set()))

    guarantee_tranche_projection: list[dict[str, Any]] = []
    for tier in ("top", "middle", "bottom", "other"):
        records = tranches_by_tier.get(tier, [])
        if not records:
            continue

        def tranche_value(field: str) -> dict[str, Any]:
            selected = next((
                (event, tranche) for event, tranche in reversed(records)
                if tranche.get(field) is not None
            ), None)
            if selected is None:
                return {
                    "value": None,
                    "source_document_ids": [],
                    "basis": "structured_source",
                }
            event, tranche = selected
            return {
                "value": tranche.get(field),
                "source_document_ids": tranche_documents(event, tranche, field),
                "basis": "structured_source",
            }

        guarantee_tranche_projection.append({
            "tier": tier,
            "commitment_amount": tranche_value("commitment_amount"),
            "fee_rate": tranche_value("fee_rate"),
            "fee_amount": tranche_value("fee_amount"),
            "utilized_amount": tranche_value("utilized_amount"),
        })

    opening_events = [event for event in events if (
        str(event.get("lifecycle_stage") or "") in {
            "considering", "announced", "terms_published", "subscription_open",
        }
        or (not event.get("lifecycle_stage")
            and str(event.get("status") or "") in {
                "proposed", "announced", "conditional", "approved",
            })
    )]
    closing_events = [event for event in events if (
        str(event.get("lifecycle_stage") or "") in {
            "preliminary_outcome", "outcome_published", "registered", "settled",
        }
        or (not event.get("lifecycle_stage")
            and str(event.get("status") or "") in {"completed", "registered"})
    )]

    def latest_from(values: list[dict], field: str) -> Any:
        return next((event.get(field) for event in reversed(values)
                     if event.get(field) is not None), None)

    announced_gross = latest_from(opening_events, "gross_amount")
    if announced_gross is None:
        announced_gross = next((
            candidate.amount for candidate in sorted(
                group, key=lambda value: (value.time, value.doc["id"]), reverse=True
            )
            if candidate.status in {
                "proposed", "announced", "conditional", "approved", "subscription_period",
            }
            and candidate.amount is not None
        ), None)
    announced_net = latest_from(opening_events, "net_amount")
    actual_gross = latest_from(closing_events, "gross_amount")
    if actual_gross is None:
        actual_gross = next((
            candidate.amount for candidate in sorted(
                group, key=lambda value: (value.time, value.doc["id"]), reverse=True
            )
            if candidate.status in {"preliminary_outcome", "completed", "registered"}
            and candidate.amount is not None
        ), None)
    actual_net = latest_from(closing_events, "net_amount")
    announced_net_basis = "source_reported" if announced_net is not None else None
    estimated_net = None
    if announced_net is None and announced_gross is not None and total_cost is not None:
        estimated_net = max(0.0, float(announced_gross) - float(total_cost))
    net_amount = actual_net if actual_net is not None else (
        announced_net if announced_net is not None else estimated_net
    )
    net_basis = ("source_reported_actual" if actual_net is not None
                 else announced_net_basis or (
                     "derived_gross_less_source_costs" if estimated_net is not None else None
                 ))
    credit_limit = latest("credit_limit")
    if credit_limit is None:
        credit_limit = next((candidate.amount for candidate in reversed(group)
                             if candidate.basis == "credit_limit" and candidate.amount is not None), None)
    drawn_amount = latest("drawn_amount")
    if drawn_amount is None:
        drawn_amount = next((candidate.amount for candidate in reversed(group)
                             if candidate.basis == "drawn_loan" and candidate.amount is not None), None)
    undrawn_commitment = latest("undrawn_commitment")
    if (undrawn_commitment is None and isinstance(credit_limit, (int, float))
            and isinstance(drawn_amount, (int, float))):
        undrawn_commitment = max(0.0, float(credit_limit) - float(drawn_amount))
    if undrawn_commitment is None and credit_limit is not None and drawn_amount is None:
        undrawn_commitment = credit_limit

    cash_fee_pct = latest("guarantee_fee_cash_pct")
    share_fee_pct = latest("guarantee_fee_share_pct")
    fee_basis = latest("guarantee_fee_basis")
    fee_clauses = [
        sentence for sentence in _sentences(combined_text)
        if re.search(
            r"garantiersätt|garantiarvod|guarantee\s+(?:remuneration|compensation|fee)",
            sentence, re.I,
        )
        and not re.search(r"aktiekapital|share\s+capital", sentence, re.I)
    ]
    fee_text = " ".join(filter(None, (str(fee_details or ""), *fee_clauses)))
    if cash_fee_pct is None:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:%|procent)[^.!?]{0,35}(?:kontant|cash)", fee_text, re.I)
        if match:
            cash_fee_pct = _numeric(match.group(1)) / 100
    if share_fee_pct is None:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:%|procent)[^.!?]{0,35}(?:aktier|shares)", fee_text, re.I)
        if match:
            share_fee_pct = _numeric(match.group(1)) / 100
    if fee_basis is None and (cash_fee_pct is not None or share_fee_pct is not None):
        fee_basis = "guaranteed_amount"
    actual_subscription = latest_from(closing_events, "final_subscription_amount")
    guarantee_unused = None
    guarantee_commitment = stored_or_source("guarantee_commitment_amount")
    guarantee_used = latest("guarantee_utilized_amount")
    if (isinstance(guarantee_commitment, (int, float)) and not isinstance(guarantee_commitment, bool)
            and isinstance(guarantee_used, (int, float)) and not isinstance(guarantee_used, bool)):
        # This is arithmetic, not a claim that a particular guarantor was
        # released.  Negative source combinations remain visible for review.
        guarantee_unused = max(0.0, float(guarantee_commitment) - float(guarantee_used))

    stage_records = []
    for event in events:
        populated = {
            field: event.get(field) for field in (
                "gross_amount", "net_amount", "transaction_cost",
                "subscription_commitment_amount", "guarantee_commitment_amount",
                "final_subscription_amount", "guarantee_utilized_amount",
            ) if event.get(field) is not None
        }
        if event.get("lifecycle_stage") or populated:
            stage_records.append({
                "lifecycle_stage": event.get("lifecycle_stage"),
                "effective_at": event.get("stage_effective_at") or event.get("effective_date")
                    or event.get("announcement_date"),
                "source_document_ids": sorted(event_documents.get(int(event["id"]), set())),
                "reported_values": populated,
            })

    reconciliation = {
        "version": "source_bound_round_v1",
        "stages": stage_records,
        "terms": {
            "announced_gross_amount": source_value("gross_amount", announced_gross, scope=opening_events),
            "announced_net_amount": source_value(
                "net_amount", announced_net,
                scope=opening_events,
            ),
            "issue_price": source_value(
                "issue_price", stored_or_source("issue_price"),
                fallback=latest("issue_price") is None and source_terms.get("issue_price") is not None,
            ),
            "subscription_commitment_amount": source_value(
                "subscription_commitment_amount", stored_or_source("subscription_commitment_amount"),
                fallback=latest("subscription_commitment_amount") is None
                    and source_terms.get("subscription_commitment_amount") is not None,
            ),
            "subscription_commitment_pct": source_value(
                "subscription_commitment_pct", stored_or_source("subscription_commitment_pct"),
                fallback=latest("subscription_commitment_pct") is None
                    and source_terms.get("subscription_commitment_pct") is not None,
            ),
            "guarantee_commitment_amount": source_value(
                "guarantee_commitment_amount", stored_or_source("guarantee_commitment_amount"),
                fallback=latest("guarantee_commitment_amount") is None
                    and source_terms.get("guarantee_commitment_amount") is not None,
            ),
            "guarantee_commitment_pct": source_value(
                "guarantee_commitment_pct", stored_or_source("guarantee_commitment_pct"),
                fallback=latest("guarantee_commitment_pct") is None
                    and source_terms.get("guarantee_commitment_pct") is not None,
            ),
        },
        "actual_outcome": {
            "gross_amount": source_value("gross_amount", actual_gross, scope=closing_events),
            "net_amount": source_value("net_amount", actual_net, scope=closing_events),
            "shares_issued": source_value(
                "shares_issued", stored_or_source("shares_issued"),
                fallback=latest("shares_issued") is None
                    and source_terms.get("shares_issued") is not None,
                scope=closing_events,
            ),
            "final_subscription_amount": source_value(
                "final_subscription_amount", actual_subscription, scope=closing_events,
            ),
            "final_subscription_pct": source_value(
                "final_subscription_pct", stored_or_source("final_subscription_pct"),
                fallback=latest("final_subscription_pct") is None
                    and source_terms.get("final_subscription_pct") is not None,
                scope=closing_events,
            ),
            "subscription_with_rights_pct": source_value(
                "subscription_with_rights_pct",
                source_terms.get("subscription_with_rights_pct"), fallback=True,
                scope=closing_events,
            ),
            "subscription_without_rights_pct": source_value(
                "subscription_without_rights_pct",
                source_terms.get("subscription_without_rights_pct"), fallback=True,
                scope=closing_events,
            ),
            "guarantee_utilized_amount": source_value(
                "guarantee_utilized_amount", guarantee_used, scope=closing_events,
            ),
            "guarantee_utilized_pct": source_value(
                "guarantee_utilized_pct", latest("guarantee_utilized_pct"), scope=closing_events,
            ),
        },
        "costs_and_net_proceeds": {
            "transaction_cost": source_value(
                "transaction_cost", transaction_cost,
                fallback=latest("transaction_cost") is None and source_terms.get("transaction_cost") is not None,
            ),
            "guarantee_fee_amount": source_value(
                "guarantee_fee_amount", fee_amount,
                fallback=latest("guarantee_fee_amount") is None and parsed_fee_amount is not None,
            ),
            "total_cost": source_value("transaction_cost", total_cost, derived=cost_basis == "source_reported_excluding_guarantee_fee"),
            "net_amount": source_value(
                "net_amount", net_amount,
                derived=net_basis == "derived_gross_less_source_costs",
            ),
            "announced_net_amount": source_value(
                "net_amount", announced_net,
                scope=opening_events,
            ),
            "estimated_net_amount": source_value(
                "net_amount", estimated_net, derived=estimated_net is not None,
                scope=opening_events,
            ),
            "actual_net_amount": source_value("net_amount", actual_net, scope=closing_events),
        },
        "guarantee_tranches": guarantee_tranche_projection,
        "derived": {
            "announced_to_actual_gross_pct": (
                round(float(actual_gross) / float(announced_gross) * 100, 1)
                if isinstance(announced_gross, (int, float)) and not isinstance(announced_gross, bool)
                and float(announced_gross) > 0
                and isinstance(actual_gross, (int, float)) and not isinstance(actual_gross, bool)
                else None
            ),
            "unused_guarantee_commitment_amount": guarantee_unused,
        },
    }
    return {
        "canonical_round_key": normalized_round_key,
        "canonical_round_keys": round_keys,
        "source_canonical_round_keys": source_round_keys,
        "canonical_round_normalized": len(source_round_keys) > 1,
        "gross_amount": gross_amount,
        "announced_gross_amount": announced_gross,
        "actual_gross_amount": actual_gross,
        "net_amount": net_amount,
        "announced_net_amount": announced_net,
        "estimated_net_amount": estimated_net,
        "actual_net_amount": actual_net,
        "net_amount_basis": net_basis,
        "net_amount_estimated": net_basis == "derived_gross_less_source_costs",
        "transaction_cost": transaction_cost,
        "transaction_cost_pct": transaction_cost_pct,
        "transaction_cost_total": total_cost,
        "transaction_cost_basis": cost_basis,
        "guarantee_fee_amount": fee_amount,
        "guarantee_fee_pct": fee_pct,
        "guarantee_fee_details": fee_details,
        "guarantee_fee_details_source_document_id": fee_details_document,
        "guarantee_fee_options": {
            "cash_fee_pct": cash_fee_pct,
            "share_fee_pct": share_fee_pct,
            "fee_basis": fee_basis,
            "share_fee_cash_effect_amount": 0.0 if share_fee_pct is not None else None,
        },
        "credit_limit": credit_limit,
        "drawn_amount": drawn_amount,
        "confirmed_cash_effect_amount": drawn_amount if drawn_amount is not None else (
            0.0 if credit_limit is not None else None
        ),
        "undrawn_commitment": undrawn_commitment,
        "issue_price": stored_or_source("issue_price"),
        "subscription_commitment_amount": stored_or_source("subscription_commitment_amount"),
        "subscription_commitment_pct": stored_or_source("subscription_commitment_pct"),
        "guarantee_commitment_amount": stored_or_source("guarantee_commitment_amount"),
        "guarantee_commitment_pct": stored_or_source("guarantee_commitment_pct"),
        "secured_total_amount": latest("secured_total_amount"),
        "secured_total_pct": latest("secured_total_pct"),
        "final_subscription_amount": latest("final_subscription_amount"),
        "final_subscription_pct": stored_or_source("final_subscription_pct"),
        "shares_issued": stored_or_source("shares_issued"),
        "subscription_with_rights_pct": source_terms.get("subscription_with_rights_pct"),
        "subscription_without_rights_pct": source_terms.get("subscription_without_rights_pct"),
        "subscription_without_guarantees_pct": stored_or_source("subscription_without_guarantees_pct"),
        "guarantee_utilized_amount": latest("guarantee_utilized_amount"),
        "guarantee_utilized_pct": latest("guarantee_utilized_pct"),
        "guarantee_take_up_pct_of_issue": source_terms.get("guarantee_take_up_pct_of_issue"),
        "guarantee_tranches": guarantee_tranche_projection,
        "adviser": latest("adviser"),
        "issuer_agent": latest("issuer_agent"),
        "outcome": latest("outcome"),
        "round_reconciliation": reconciliation,
    }


def _item(group: list[_Candidate], participants: dict[int, list[dict]]) -> dict:
    # Preserve every source version, including translations, but prefer Swedish
    # when the latest source has a Swedish edition in the same translation set.
    group.sort(key=lambda c: (c.time, _language(c) == "sv", c.doc["id"]))
    latest = group[-1]
    swedish_editions = [
        candidate for candidate in group
        if _language(candidate) == "sv" and _translations(candidate, latest)
    ]
    if swedish_editions:
        latest = max(swedish_editions, key=lambda candidate: (candidate.time, candidate.doc["id"]))
    lifecycle_order = {
        "proposed": 0,
        "announced": 1,
        "conditional": 2,
        "approved": 3,
        "subscription_period": 4,
        "preliminary_outcome": 5,
        "completed": 6,
        "registered": 7,
    }
    cancelled = next(
        (candidate for candidate in reversed(group) if candidate.status == "cancelled"),
        None,
    )
    if cancelled is None:
        progressed = [
            candidate for candidate in group
            if candidate.status in lifecycle_order
        ]
        if progressed:
            effective = max(
                progressed,
                key=lambda candidate: (
                    lifecycle_order[candidate.status],
                    candidate.time,
                    candidate.doc["id"],
                ),
            )
            # Keep the newest source as presentation source, but never let a
            # later reminder/prospectus regress an outcome to "updated".
            latest_status = effective.status
        else:
            latest_status = latest.status
    else:
        latest_status = "cancelled"
    # The feed card describes the financing round, not a selected commitment
    # from a later terms release.  Choose an evidenced outcome first, then the
    # announced round size.  A subscription/guarantee commitment is never a
    # substitute for either amount.
    amount_priority = (
        {"completed", "registered"},
        {"preliminary_outcome"},
        {"proposed", "announced", "conditional", "approved", "subscription_period"},
    )
    round_amount_source = next((
        max(
            (
                candidate for candidate in group
                if candidate.status in statuses and candidate.amount is not None
                and candidate.basis not in {"maximum_proceeds", "credit_limit"}
            ),
            key=lambda candidate: (candidate.time, candidate.doc["id"]),
            default=None,
        )
        for statuses in amount_priority
        if any(candidate.status in statuses and candidate.amount is not None
               and candidate.basis not in {"maximum_proceeds", "credit_limit"}
               for candidate in group)
    ), None)
    if round_amount_source is None:
        round_amount_source = latest
    # Business contract: a priced, decided directed issue is treated as cash
    # raised immediately. Source lifecycle status remains intact; the derived
    # 100% outcome is marked separately in details below.
    directed_source = next((
        candidate for candidate in reversed(group)
        if candidate.category == "directed_issue"
        and candidate.status in {"announced", "approved", "completed", "registered"}
        and candidate.amount is not None and candidate.amount > 0
        and candidate.basis not in {"maximum_proceeds", "credit_limit"}
        and not re.search(
            r"kvittning|apport|ingen kontant likvid|utan kontant (?:betalning|likvid)|"
            r"no cash (?:consideration|proceeds)|\bset[-\s]+off\b|"
            r"\bnon[-\s]+cash\b|\bin[-\s]+kind\b|\bshares?[-\s]+for[-\s]+debt\b",
            candidate.text,
            re.I,
        )
    ), None) if latest.category == "directed_issue" else None
    directed_cash_outcome = directed_source is not None
    if directed_source is not None and round_amount_source.amount is None:
        round_amount_source = directed_source
    doc = latest.doc
    institutions = _institutions(group, participants)
    dossier = _financing_dossier(group)
    for field, role in (
        ("adviser", "Finansiell rådgivare"),
        ("issuer_agent", "Emissionsinstitut"),
    ):
        name = str(dossier.get(field) or "").strip()
        if name and not any(item.get("role") == role for item in institutions):
            institutions.append({"name": name, "role": role})
    label = CATEGORY_LABELS[latest.category]
    status_label = STATUS_LABELS[latest_status]
    signal_tone, signal_label = _signal(
        group, latest.category, latest_status,
        directed_cash_outcome=directed_cash_outcome,
    )
    date = doc["published_at"][:10]
    summary = f"{doc['issuer_name']} publicerade den {date} ett besked om {label.lower()}. Status enligt det senaste källmeddelandet: {status_label.lower()}."
    if round_amount_source.amount is not None:
        value = (f"{round_amount_source.amount:,.0f}" if round_amount_source.amount.is_integer()
                 else f"{round_amount_source.amount:,.2f}")
        value = value.replace(",", " ").replace(".", ",")
        basis_labels = {
            "announced_amount": "Offentliggjort belopp", "outcome_amount": "Redovisat utfallsbelopp",
            "announced_gross": "Offentliggjort bruttobelopp", "announced_net": "Offentliggjort nettobelopp",
            "outcome_gross": "Redovisat bruttoutfall", "outcome_net": "Redovisat nettoutfall",
            "maximum_proceeds": "Högsta angivna belopp", "loan_principal": "Angivet lånebelopp",
            "credit_limit": "Angiven kreditram",
        }
        summary += f" {basis_labels.get(round_amount_source.basis, 'Angivet belopp')}: {value} {round_amount_source.currency}."
    else:
        summary += " Belopp kan inte fastställas entydigt från det tillgängliga källunderlaget."
    if institutions:
        summary += " " + "; ".join(f"{p['name']} anges som {p['role'].lower()}" for p in institutions[:2]) + "."
    else:
        summary += " Finansiella institutioner och deras transaktionsroller är inte verifierade."
    history = [{"document_id": c.doc["id"], "published_at": c.doc["published_at"],
                "title": c.doc["title"], "url": c.doc["source_url"], "status": c.status}
               for c in reversed(group)]
    announced = next((
        candidate for candidate in reversed(group)
        if candidate.status in {"proposed", "announced", "conditional", "approved", "subscription_period"}
        and candidate.amount is not None
    ), None)
    outcome = next((
        candidate for candidate in reversed(group)
        if candidate.status in {"preliminary_outcome", "completed", "registered"}
        and candidate.amount is not None
    ), None)
    derived_directed_outcome = directed_cash_outcome and outcome is None
    if derived_directed_outcome:
        outcome = directed_source
    currencies = {candidate.currency for candidate in (announced, outcome) if candidate and candidate.currency}
    completion_ratio = None
    if (announced and outcome and announced.amount and outcome.amount is not None
            and len(currencies) == 1):
        completion_ratio = round(outcome.amount / announced.amount * 100, 1)
    lifecycle = [{
        **entry,
        "status_label": STATUS_LABELS.get(entry["status"], entry["status"]),
    } for entry in reversed(history)]
    lifecycle_stage = next((
        stage.get("lifecycle_stage")
        for stage in reversed((dossier.get("round_reconciliation") or {}).get("stages") or [])
        if stage.get("lifecycle_stage")
    ), None)
    confirmed_cash_effect = next((
        event.get("cash_effect_amount")
        for candidate in reversed(group)
        for event in reversed(candidate.events)
        if event.get("liquidity_effect_kind") == "confirmed_cash"
        and event.get("cash_effect_amount") is not None
    ), None)
    if latest.category == "credit_facility":
        # A facility limit is capacity, not cash. Only a source-backed draw is
        # allowed to become confirmed liquidity.
        confirmed_cash_effect = dossier.get("drawn_amount")
        if confirmed_cash_effect is None:
            confirmed_cash_effect = 0.0
    equity_or_warrant = latest.category in {
        "rights_issue", "directed_issue", "unit_issue", "warrant_exercise", "other_equity",
    }
    completeness_values = {
        "amount": round_amount_source.amount,
        "currency": round_amount_source.currency,
        "amount_basis": round_amount_source.basis,
        "source": bool(group),
    }
    missing_required_fields = [
        name for name, value in completeness_values.items()
        if value is None or value is False or (isinstance(value, str) and not value.strip())
    ] if equity_or_warrant else []
    round_key_conflict = (
        len(dossier.get("source_canonical_round_keys") or []) > 1
        and not dossier.get("canonical_round_normalized")
    )
    details = {
        "overview": summary,
        "announced_amount": announced.amount if announced else None,
        "announced_amount_basis": announced.basis if announced else None,
        "outcome_amount": outcome.amount if outcome else None,
        "outcome_amount_basis": (
            "directed_issue_cash_assumption"
            if derived_directed_outcome
            else outcome.basis if outcome else None
        ),
        # Rights issues in an opening stage have no confirmed cash inflow yet.
        # Keep advertised gross proceeds separate from actual liquidity.
        "confirmed_cash_effect_amount": (
            confirmed_cash_effect
            if confirmed_cash_effect is not None
            else directed_source.amount if derived_directed_outcome else 0.0
        ),
        "currency": next(iter(currencies), latest.currency),
        "completion_ratio_pct": completion_ratio,
        "source_count": len(history),
        "lifecycle": lifecycle,
        "detail_summary": None,
        "key_points": [],
        "terms": [],
        "use_of_proceeds": None,
        "next_milestone": None,
        "financing_dossier": dossier,
        "lifecycle_stage": lifecycle_stage,
        "transaction_completeness": {
            "status": "incomplete" if missing_required_fields else "complete",
            "required_fields": ["amount", "currency", "amount_basis", "source"],
            "missing_fields": missing_required_fields,
        },
        "canonical_round_conflict": round_key_conflict,
    }
    return {
        "id": f"transaction:{doc['issuer_id']}:{latest.category}:{min(c.doc['id'] for c in group)}",
        "issuer_id": doc["issuer_id"], "issuer_name": doc["issuer_name"],
        "category": latest.category, "category_label": label,
        "status": latest_status, "status_label": status_label,
        "signal_tone": signal_tone, "signal_label": signal_label,
        "headline": f"{doc['issuer_name']}: {label.lower()} – {status_label.lower()}",
        "summary": summary, "editorial_source": "source",
        "amount": round_amount_source.amount, "currency": round_amount_source.currency,
        "amount_basis": round_amount_source.basis,
        "institutions": institutions, "published_at": doc["published_at"],
        "source_url": doc["source_url"], "source_document_ids": sorted({c.doc['id'] for c in group}),
        "history": history, "details": details,
        "review_required": (
            any(c.review for c in group) or bool(missing_required_fields)
            or round_amount_source.amount is None or round_key_conflict
        ),
    }


def _project_transaction_values(item: dict[str, Any], patch: dict[str, Any]) -> None:
    """Project reviewed feed facts while retaining the source-derived card."""
    values = patch.get("values") or {}
    details = dict(item.get("details") or {})
    dossier = dict(details.get("financing_dossier") or {})
    original = {
        "category": item.get("category"),
        "status": item.get("status"),
        "lifecycle_stage": details.get("lifecycle_stage"),
        "amount": item.get("amount"),
        "amount_basis": item.get("amount_basis"),
        "currency": item.get("currency"),
        "confirmed_cash_effect_amount": details.get("confirmed_cash_effect_amount"),
        "canonical_round_key": dossier.get("canonical_round_key"),
        "actual_gross_amount": dossier.get("actual_gross_amount"),
        "additional_instruments": None,
        "effective_stages": (dossier.get('round_reconciliation') or {}).get('stages', []),
    }
    from .transaction_value_corrections import TERM_FIELDS, _value
    original.update({field:_value(item,field) for field in TERM_FIELDS})
    effective = {**original, **{field: value for field, value in values.items()
                               if field in original}}
    item["original_transaction_values"] = original
    item["effective_transaction_values"] = effective
    item["applied_transaction_correction_ids"] = sorted(set(
        int(value) for value in patch.get("correction_ids", [])
    ))
    item["effective_corrected_fields"] = sorted(field for field in values if field in original)
    if not values:
        return
    for field in ("category", "status", "amount", "amount_basis", "currency"):
        item[field] = effective[field]
    item["category_label"] = CATEGORY_LABELS.get(str(item["category"]), str(item["category"]))
    item["status_label"] = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
    if "confirmed_cash_effect_amount" in values:
        details["confirmed_cash_effect_amount"] = effective["confirmed_cash_effect_amount"]
        dossier["confirmed_cash_effect_amount"] = effective["confirmed_cash_effect_amount"]
    if "canonical_round_key" in values:
        dossier["canonical_round_key"] = effective["canonical_round_key"]
        dossier["canonical_round_keys"] = ([effective["canonical_round_key"]]
                                            if effective["canonical_round_key"] else [])
    if "actual_gross_amount" in values:
        dossier["actual_gross_amount"] = effective["actual_gross_amount"]
    if "effective_stages" in values:
        reconciliation = dict(dossier.get('round_reconciliation') or {})
        reconciliation['original_stages'] = reconciliation.get('stages', [])
        reconciliation['stages'] = effective['effective_stages']
        reconciliation['stage_correction_ids'] = item['applied_transaction_correction_ids']
        dossier['round_reconciliation'] = reconciliation
    for field in TERM_FIELDS & values.keys():
        dossier[field] = effective[field]
    # The reconciliation panel is an effective view too. Retain the extraction
    # alongside it rather than displaying superseded facts as current outcomes.
    from copy import deepcopy
    reconciliation = deepcopy(dossier.get('round_reconciliation') or {})
    for section, mapping in {
        'terms': {name: name for name in ('announced_gross_amount', 'announced_net_amount', 'issue_price',
            'subscription_commitment_amount', 'subscription_commitment_pct', 'guarantee_commitment_amount', 'guarantee_commitment_pct')},
        'actual_outcome': {'actual_gross_amount': 'gross_amount', 'actual_net_amount': 'net_amount',
            'confirmed_cash_effect_amount': 'confirmed_cash_effect_amount',
            **{name: name for name in ('shares_issued', 'final_subscription_amount', 'final_subscription_pct',
                'subscription_with_rights_pct', 'subscription_without_rights_pct', 'guarantee_utilized_amount', 'guarantee_utilized_pct')}},
        'costs_and_net_proceeds': {name: name for name in ('transaction_cost', 'guarantee_fee_amount',
            'net_amount', 'announced_net_amount', 'estimated_net_amount', 'actual_net_amount')},
    }.items():
        changed = {field: alias for field, alias in mapping.items() if field in values}
        if changed:
            reconciliation['original_' + section] = deepcopy(reconciliation.get(section, {}))
            facts = reconciliation.setdefault(section, {})
            for field, alias in changed.items():
                source = (patch.get('field_source_ids') or {}).get(field)
                facts[alias] = {'value': effective[field], 'basis': 'audited_effective_overlay',
                    'source_document_ids': [source] if source else item.get('source_document_ids', []),
                    'correction_id': (patch.get('field_correction_ids') or {}).get(field)}
    if 'actual_gross_amount' in values or 'announced_gross_amount' in values:
        actual, announced = dossier.get('actual_gross_amount'), dossier.get('announced_gross_amount')
        reconciliation.setdefault('derived', {})['announced_to_actual_gross_pct'] = (
            100 * actual / announced if isinstance(actual, (int, float)) and isinstance(announced, (int, float)) and announced > 0 else None)
    dossier['round_reconciliation'] = reconciliation
    options = dict(dossier.get('guarantee_fee_options') or {})
    for field,alias in {'guarantee_fee_cash_pct':'cash_fee_pct','guarantee_fee_share_pct':'share_fee_pct','guarantee_fee_basis':'fee_basis'}.items():
        if field in values:
            options[alias]=effective[field]
    dossier['guarantee_fee_options']=options
    if "lifecycle_stage" in values:
        details["lifecycle_stage"] = effective["lifecycle_stage"]
        dossier["effective_lifecycle_stage"] = effective["lifecycle_stage"]
    if any(field in values for field in ("amount", "amount_basis", "currency", "status")):
        if str(item.get("status")) in {"completed", "registered", "preliminary_outcome"}:
            details["outcome_amount"] = item["amount"]
            details["outcome_amount_basis"] = item["amount_basis"]
        else:
            details["announced_amount"] = item["amount"]
            details["announced_amount_basis"] = item["amount_basis"]
        details["currency"] = item["currency"]
    # A reviewed gross announcement must also replace its matching gross
    # display fact. Net proceeds, facility limits and commitments are distinct.
    gross_announced_bases = {"announced_amount", "announced_gross", "maximum_proceeds"}
    if ('announced_gross_amount' in values
            and details.get('announced_amount_basis') in gross_announced_bases):
        details['original_announced_amount'] = details.get('announced_amount')
        details['announced_amount'] = effective['announced_gross_amount']
    # Recalculate only an already comparable ratio; a missing ratio may encode
    # mixed currencies or unknown amounts and must remain unknown.
    gross_outcome_bases = {"preliminary_outcome", "outcome_amount", "outcome_gross",
                           "directed_issue_cash_assumption"}
    if (any(field in values for field in ('announced_gross_amount', 'amount', 'amount_basis'))
            and details.get('completion_ratio_pct') is not None
            and details.get('announced_amount_basis') in gross_announced_bases
            and details.get('outcome_amount_basis') in gross_outcome_bases):
        announced = details.get('announced_amount')
        outcome = details.get('outcome_amount')
        details['original_completion_ratio_pct'] = details['completion_ratio_pct']
        details['completion_ratio_pct'] = (
            round(outcome / announced * 100, 1)
            if isinstance(announced, (int, float)) and announced > 0
            and isinstance(outcome, (int, float)) else None)
    details["financing_dossier"] = dossier
    if item.get("category") in {
        "rights_issue", "directed_issue", "unit_issue", "warrant_exercise", "other_equity",
    }:
        values_for_check = {
            "amount": item.get("amount"), "currency": item.get("currency"),
            "amount_basis": item.get("amount_basis"),
            "source": bool(item.get("source_document_ids")),
        }
        missing = [name for name, value in values_for_check.items()
                   if value is None or value is False
                   or (isinstance(value, str) and not value.strip())]
        details["transaction_completeness"] = {
            "status": "incomplete" if missing else "complete",
            "required_fields": list(values_for_check), "missing_fields": missing,
        }
        item["review_required"] = (
            bool(item.get("review_required")) or bool(missing)
            or bool(details.get("canonical_round_conflict"))
        )
    item["details"] = details
    label = item["category_label"].lower()
    status_label = item["status_label"].lower()
    item["headline"] = f"{item['issuer_name']}: {label} – {status_label}"
    summary = (f"{item['issuer_name']} publicerade den {str(item.get('published_at') or '')[:10]} "
               f"ett besked om {label}. Status: {status_label}.")
    amount = item.get("amount")
    if isinstance(amount, (int, float)) and math.isfinite(amount):
        formatted = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
        summary += f" Belopp: {formatted.replace(',', ' ').replace('.', ',')} {item.get('currency') or ''}."
    item["summary"] = summary
    details["overview"] = summary


def _transaction_store_signature(connection: sqlite3.Connection) -> tuple[str, tuple[tuple[int, int], tuple[int, int]]] | None:
    """Return a cheap signature that changes on committed SQLite/WAL writes."""
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    path = str(row[2] if not isinstance(row, sqlite3.Row) else row["file"] or "")
    if not path or path == ":memory:":
        return None

    def stamp(value: str) -> tuple[int, int]:
        try:
            info = os.stat(value)
            return info.st_mtime_ns, info.st_size
        except OSError:
            return 0, 0

    return os.path.abspath(path), (stamp(path), stamp(path + "-wal"))


def _build_transaction_items(connection: sqlite3.Connection, *,
                             check_cancelled: Callable[[], None] | None = None,
                             issuer_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Build the canonical, source-linked transaction set once per read."""
    tables = {r["name"] for r in _rows(connection, "SELECT name FROM sqlite_master WHERE type='table'")}
    # Existing Phase 3 tables are required, historical enrichment is optional.
    documents = _rows(connection, """
        SELECT d.id, d.issuer_id, i.canonical_name AS issuer_name, d.title,
               d.published_at, d.language, d.canonical_url, d.profile_category
        FROM documents d JOIN issuers i ON i.id=d.issuer_id
        WHERE (EXISTS (SELECT 1 FROM document_sources s
                      WHERE s.document_id=d.id AND s.source_id='mfn')
           OR EXISTS (SELECT 1 FROM financing_event_evidence e WHERE e.document_id=d.id))
    """ + (" AND d.issuer_id IN (" + ",".join("?" for _ in issuer_ids) + ")" if issuer_ids else
             " AND 0" if issuer_ids == [] else ""), issuer_ids or ())
    document_round_links = effective_document_round_links(
        connection, [int(document["id"]) for document in documents]
    )
    document_rounds = effective_document_rounds(
        connection, [int(document["id"]) for document in documents]
    )
    from .instrument_grouping import bindings as instrument_bindings
    scoped_bindings = [b for b in instrument_bindings(connection,issuer_ids) if b['source_current']]
    scoped_documents = {}
    for b in scoped_bindings:
        for doc in b['document_ids']:
            scoped_documents[(doc,b['category'])] = b
    projection_bindings = effective_projection_bindings(connection, document_rounds)
    from .grouping_consolidation import bindings as consolidation_bindings
    extra_bindings = consolidation_bindings(connection,document_rounds)
    if any(set(a['document_ids']) & set(b['document_ids']) for a in projection_bindings for b in extra_bindings):
        raise ValueError('grouping_consolidation_ambiguous')
    projection_bindings += extra_bindings
    changed_neutral_documents = {doc for binding in projection_bindings for doc in binding['changed_neutral_document_ids']}
    bound_documents = {doc: binding for binding in projection_bindings for doc in binding['document_ids'] if doc not in changed_neutral_documents}
    neutral_documents = {doc for binding in projection_bindings for doc in binding['neutral_document_ids'] if doc not in changed_neutral_documents}
    reports = {r["document_id"] for r in _rows(connection,
               "SELECT document_id FROM report_extractions WHERE is_financial_report=1")}
    documents = [d for d in documents if (d["id"] not in reports
                                           or document_round_links.get(int(d["id"]))
                                           or document_rounds.get(int(d["id"])))
                 and (d["profile_category"] not in {"report", "calendar"}
                      or document_round_links.get(int(d["id"]))
                      or document_rounds.get(int(d["id"])))
                 and (classify_title(d["title"]).profile_category not in {"report", "calendar"}
                      or document_round_links.get(int(d["id"]))
                      or document_rounds.get(int(d["id"])))
                 and (not _EXCLUDE.search(d["title"])
                      or document_round_links.get(int(d["id"]))
                      or document_rounds.get(int(d["id"])))
                 and (_categories(d["title"]) or _GENERIC.search(d["title"])
                      or document_round_links.get(int(d["id"]))
                      or document_rounds.get(int(d["id"]))) ]
    if not documents:
        return []
    for document in documents:
        document["effective_round_key"] = document_rounds.get(int(document["id"]))
        if document['id'] in changed_neutral_documents:
            document['effective_round_key'] = None
            document['_binding_source_changed'] = True
    sources: dict[int, list[dict]] = defaultdict(list)
    contents: dict[int, dict] = {}
    events: dict[int, list[dict]] = defaultdict(list)
    for ids in _batches(d["id"] for d in documents):
        if check_cancelled:
            check_cancelled()
        slots = ",".join("?" for _ in ids)
        for row in _rows(connection, f"SELECT * FROM document_sources WHERE document_id IN ({slots})", ids):
            sources[row["document_id"]].append(row)
        # Rank lightweight metadata in SQLite, then load only the chosen text.
        # A document may have several large extracted attachment versions.
        for row in _rows(connection, f"""
                SELECT c.document_id, c.extracted_text
                FROM document_contents c
                JOIN (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY document_id
                        ORDER BY (attachment_id IS NULL) DESC, quality_score DESC, id DESC
                    ) AS rank
                    FROM document_contents
                    WHERE document_id IN ({slots})
                      AND status IN ('extracted','too_short')
                      AND extracted_text IS NOT NULL
                ) chosen ON chosen.id = c.id AND chosen.rank = 1""", ids):
            contents[row["document_id"]] = row
        missing_content_ids = [value for value in ids if value not in contents]
        if missing_content_ids and "document_evidence_texts" in tables:
            archive_slots = ",".join("?" for _ in missing_content_ids)
            for row in _rows(connection, f"""
                    SELECT evidence.document_id,
                           substr(evidence.full_text,1,120000) AS extracted_text
                    FROM document_evidence_texts evidence
                    JOIN (
                        SELECT document_id,MAX(id) AS id FROM document_evidence_texts
                        WHERE document_id IN ({archive_slots}) GROUP BY document_id
                    ) latest ON latest.id=evidence.id""", missing_content_ids):
                contents[row["document_id"]] = row
        for row in _rows(connection, f"""SELECT e.*, v.document_id, v.source_excerpt
                FROM financing_event_evidence v JOIN financing_events e ON e.id=v.financing_event_id
                WHERE v.document_id IN ({slots})""", ids):
            events[row["document_id"]].append(row)
    linked_event_ids = sorted({int(link["financing_event_id"])
                               for links in document_round_links.values() for link in links
                               if link.get("financing_event_id") is not None})
    linked_events: dict[int, dict] = {}
    for ids in _batches(linked_event_ids):
        slots = ",".join("?" for _ in ids)
        for row in _rows(connection, f"SELECT * FROM financing_events WHERE id IN ({slots})", ids):
            linked_events[int(row["id"])] = row
    issuer_by_document = {int(doc["id"]): int(doc["issuer_id"]) for doc in documents}
    for document_id, links in document_round_links.items():
        existing = {int(event["id"]) for event in events.get(document_id, [])}
        for link in links:
            event_id = link.get("financing_event_id")
            if event_id is None or int(event_id) in existing:
                continue
            event = linked_events.get(int(event_id))
            if event is None or int(event["issuer_id"]) != issuer_by_document.get(int(document_id)):
                continue
            events[document_id].append({**event, "document_id": document_id,
                                        "source_excerpt": str(event.get("description") or "")})
            existing.add(int(event_id))
    overrides = effective_overrides(
        connection, [int(event["id"]) for rows in events.values() for event in rows]
    )
    for document_events in events.values():
        for event in document_events:
            # Original extraction is immutable; active overrides are projected
            # only into this read model.
            patch = overrides.get(int(event["id"]), {})
            event.update(patch)
            event["_override_fields"] = set(patch)
    # Document grouping corrections are the final, reviewed identity layer.
    # They deliberately outrank automatic extraction and field-level patches.
    for document_id, round_key in document_rounds.items():
        if document_id in changed_neutral_documents:
            continue
        for event in events.get(document_id, []):
            event["canonical_round_key"] = round_key
            event["_transaction_grouping_override"] = True
    participants: dict[int, list[dict]] = defaultdict(list)
    if "financing_event_participants" in tables:
        for ids in _batches(e["id"] for rows in events.values() for e in rows):
            slots = ",".join("?" for _ in ids)
            for row in _rows(connection, f"SELECT * FROM financing_event_participants WHERE financing_event_id IN ({slots})", ids):
                participants[row["financing_event_id"]].append(row)
    guarantee_tranches: dict[int, list[dict]] = defaultdict(list)
    if "financing_event_guarantee_tranches" in tables:
        for ids in _batches(e["id"] for rows in events.values() for e in rows):
            slots = ",".join("?" for _ in ids)
            for row in _rows(connection, f"""SELECT *
                    FROM financing_event_guarantee_tranches
                    WHERE financing_event_id IN ({slots})
                    ORDER BY tier, id""", ids):
                guarantee_tranches[row["financing_event_id"]].append(row)
    for document_events in events.values():
        for event in document_events:
            event["guarantee_tranches"] = list(
                guarantee_tranches.get(event["id"], [])
            )
    by_issuer_category: dict[tuple[int, str], list[_Candidate]] = defaultdict(list)
    for doc in documents:
        if check_cancelled:
            check_cancelled()
        source_rows = sources[doc["id"]]
        mfn = [s for s in source_rows if s["source_id"] == "mfn"]
        source = max(mfn or source_rows, key=lambda s: (_stamp(s["source_published_at"]), s["id"]), default=None)
        doc["source_url"] = source["discovered_url"] if source else doc["canonical_url"]
        if source and source["source_published_at"]:
            doc["published_at"] = source["source_published_at"]
        content = contents.get(doc["id"])
        body = _clean(content["extracted_text"]) if content else ""
        links = document_round_links.get(int(doc["id"]), [])
        reviewed = bool(document_rounds.get(int(doc["id"])))
        if doc['id'] in neutral_documents:
            continue
        binding = bound_documents.get(doc['id'])
        if binding is not None:
            doc['linked_transaction_id'] = binding['transaction_id']
        categories = _categories(doc["title"], reviewed=reviewed)
        if binding is not None and binding.get('consolidated_value_owners'):
            categories = [binding['category']]
        if not (binding is not None and binding.get('consolidated_value_owners')):
            categories.extend(str(link["category"]) for link in links
                              if link.get("category") in CATEGORY_LABELS)
        # Generic financing headlines may use classified evidence, provided the
        # evidence identifies a current financing instead of a historical mention.
        if not categories:
            for event in events[doc["id"]]:
                category = event.get("financing_type") or _EVENT_TYPES.get(event["event_type"], event["event_type"])
                event_date = event.get("effective_date") or event.get("announcement_date")
                if (category in CATEGORY_LABELS and event_date
                        and abs((_stamp(event_date) - _stamp(doc["published_at"])).days) <= 14
                        and _RX[category].search(event["source_excerpt"] or "")):
                    categories.append(category)
        categories.extend(category for doc_id,category in scoped_documents if doc_id==doc['id'])
        for category in sorted(set(categories)):
            relevant = [e for e in events[doc["id"]]
                        if e["issuer_id"] == doc["issuer_id"]
                        and (e.get("financing_type") or _EVENT_TYPES.get(e["event_type"], e["event_type"])) == category
                        and (_RX[category].search(e["source_excerpt"] or "")
                             or any(link.get("financing_event_id") == e["id"]
                                    and (not link.get("category") or link["category"] == category)
                                    for link in links))]
            status = _transaction_status(doc["title"], body, category, reviewed=reviewed)
            category_links = [link for link in links
                              if not link.get("category") or link["category"] == category]
            scoped = scoped_documents.get((doc['id'],category))
            if scoped is not None:
                category_links = [{'canonical_round_key':scoped['canonical_round_key'],
                    'category':category,'financing_event_id':None,
                    'transaction_id':scoped['transaction_id'],'relation_action':'reviewed_grouping'}]
            if category_links and all(link.get("relation_action") == "unlink"
                                      for link in category_links):
                continue
            category_links = [link for link in category_links
                              if link.get("relation_action") != "unlink"]
            # A reviewed link creates one candidate for each document/round
            # relation.  The same source can therefore support two instruments
            # without assigning unrelated event values to both rounds.
            for link in category_links or [None]:
                candidate_events = relevant
                candidate_doc = doc
                if link is not None:
                    round_key = str(link["canonical_round_key"])
                    event_id = link.get("financing_event_id")
                    matching = [event for event in relevant
                                if event_id is not None and int(event["id"]) == int(event_id)]
                    if event_id is None:
                        matching = [event for event in relevant
                                    if str(event.get("canonical_round_key") or "") == round_key]
                    if not matching and len(category_links) == 1 and event_id is None:
                        matching = relevant
                    candidate_events = [{**event, "canonical_round_key": round_key}
                                        for event in matching]
                    if binding is not None and link.get('transaction_id') not in (None, binding['transaction_id']):
                        raise ValueError('grouping_projection_binding_link_conflict')
                    candidate_doc = {**doc, "effective_round_key": round_key,
                                     "linked_transaction_id": link.get("transaction_id") or doc.get('linked_transaction_id')}
                # Lifecycle/status overrides are the only deliberate exception
                # to title-derived lifecycle.  They remain separate from raw
                # extraction and are scoped to this round relation.
                status_override = next((str(event["status"]) for event in candidate_events
                                        if "status" in event.get("_override_fields", set())), None)
                candidate = _Candidate(candidate_doc, category, status_override or status,
                                       body, candidate_events, review=not bool(body))
                if not body and candidate_events:
                    candidate.body = " ".join(e["source_excerpt"] for e in candidate_events)
                _identity(candidate)
                _amount(candidate)
                by_issuer_category[(doc["issuer_id"], category)].append(candidate)
    items = []
    for candidates in by_issuer_category.values():
        groups: list[list[_Candidate]] = []
        for candidate in sorted(candidates, key=lambda c: (c.time, c.doc["id"])):
            if check_cancelled:
                check_cancelled()
            # A reviewed document/round identity is authoritative. Do not let
            # title, publication, series-noise or multiple-match heuristics
            # fragment it after _related has already accepted that identity.
            # Audited distinct transactions within a financing hierarchy remain
            # separate even if someone reused a round key.
            forced = str(candidate.doc.get("effective_round_key") or "").strip().casefold()
            reviewed = [g for g in groups if forced and all(
                str(c.doc.get("effective_round_key") or "").strip().casefold() == forced
                and _related(c, candidate) for c in g
            )]
            if reviewed:
                linked_ids = {str(c.doc["linked_transaction_id"])
                              for g in [*reviewed, [candidate]] for c in g
                              if c.doc.get("linked_transaction_id")}
                if len(linked_ids) > 1:
                    candidate.review = True
                    groups.append([candidate])
                    continue
                target = reviewed[0]
                for other in reviewed[1:]:
                    target.extend(other)
                    groups.remove(other)
                target.append(candidate)
                continue
            matches = [g for g in groups if any(_related(c, candidate) for c in g)]
            # A translation may also match several older, ambiguous rounds.
            # Prefer its same-publication counterpart without joining those
            # older rounds together through this new release.
            publication_matches = [g for g in matches if any(
                _translations(c, candidate) or _same_stage_update(c, candidate)
                for c in g
            )]
            if len(publication_matches) == 1:
                matches = publication_matches
            if len(matches) == 1:
                # A candidate with conflicting source identities must not become
                # a transitive bridge between different instrument series.
                series = set().union(*(c.series for c in matches[0]))
                if candidate.series and series and candidate.series != series:
                    candidate.review = True
                    groups.append([candidate])
                else:
                    matches[0].append(candidate)
            else:
                if matches:
                    candidate.review = True
                    for group in matches:
                        for existing in group:
                            existing.review = True
                groups.append([candidate])
        for group in groups:
            if check_cancelled:
                check_cancelled()
            item = _item(group, participants)
            linked_ids = {str(candidate.doc["linked_transaction_id"])
                          for candidate in group if candidate.doc.get("linked_transaction_id")}
            if len(linked_ids) == 1:
                item["id"] = linked_ids.pop()
                item["_identity_from_link"] = True
            items.append(item)
    # Legacy IDs are document-based.  Only a document with several relations
    # in the same category can collide; suffix those rows by reviewed round
    # identity while keeping ordinary transaction IDs unchanged.
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_id[item["id"]].append(item)
    for duplicate_items in by_id.values():
        if len(duplicate_items) < 2:
            continue
        anchored = [item for item in duplicate_items if item.get("_identity_from_link")]
        for item in duplicate_items:
            if len(anchored) == 1 and item is anchored[0]:
                continue
            round_key = str((item.get("details") or {}).get("financing_dossier", {}).get("canonical_round_key") or "")
            if not round_key:
                raise ValueError("Ambiguous transaction identity for shared document")
            suffix = hashlib.sha256(round_key.encode("utf-8")).hexdigest()[:12]
            item["id"] = f"{item['id']}:round:{suffix}"
    for item in items:
        item.pop("_identity_from_link", None)
    # Audited source membership is independent of headline classification.
    # Attach otherwise unclassified translations/lifecycle notices to an
    # existing, unambiguous round without parsing new amounts or creating events.
    round_items: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for item in items:
        key = (item.get('details') or {}).get('financing_dossier', {}).get('canonical_round_key')
        if key:
            round_items[(item['issuer_id'], key)].append(item)
    for doc in documents:
        if check_cancelled:
            check_cancelled()
        round_key = document_rounds.get(int(doc['id']))
        if int(doc['id']) in changed_neutral_documents:
            continue
        if not round_key:
            continue
        matches = round_items.get((doc['issuer_id'], round_key), [])
        if len(matches) != 1:
            continue
        item = matches[0]
        if doc['id'] in item['source_document_ids']:
            continue
        item['source_document_ids'] = sorted([*item['source_document_ids'], doc['id']])
        item['history'].append({'document_id': doc['id'], 'published_at': doc['published_at'],
                                'title': doc['title'], 'url': doc['source_url'],
                                'status': 'related_source'})
        item['history'].sort(key=lambda entry: (entry['published_at'], entry['document_id']), reverse=True)
        item['details']['source_count'] = len(item['history'])
    # Keep the correction-owned identity/provenance explicit in the published
    # card. Retired scalar identities are never silently copied or discarded.
    for binding in projection_bindings:
        retired = effective_transaction_overrides(connection, binding['retired_transaction_ids'])
        if any(patch.get('values') for patch in retired.values()):
            raise ValueError('grouping_rebuild_retired_value_override')
        matches = [item for item in items if item['id'] == binding['transaction_id']]
        if len(matches) != 1:
            raise ValueError('grouping_projection_binding_target_mismatch')
        if binding.get('consolidated_value_owners'):
            matches[0]['source_value_owners'] = binding['value_owners']
        matches[0]['grouping_projection_binding'] = {
            'correction_id': binding['correction_id'], 'transaction_id': binding['transaction_id'],
            'canonical_round_key': binding['canonical_round_key'],
            'document_ids': binding['document_ids'], 'neutral_document_ids': binding['neutral_document_ids'],
            'retired_transaction_ids': binding['retired_transaction_ids'],
            'changed_neutral_document_ids': binding['changed_neutral_document_ids'],
            'legacy_neutral_financing_event_ids': binding['legacy_neutral_financing_event_ids'],
        }
    value_overrides: dict[str, dict[str, Any]] = {}
    for ids in _batches(str(item["id"]) for item in items):
        value_overrides.update(effective_transaction_overrides(connection, ids))
    for item in items:
        _project_transaction_values(item, value_overrides.get(str(item["id"]), {}))
    additions = []
    round_keys = {(item['issuer_id'], str((item.get('details') or {}).get('financing_dossier', {}).get('canonical_round_key') or '').casefold())
                  for item in items}
    for parent in items:
        for instrument in (parent.get('effective_transaction_values') or {}).get('additional_instruments') or []:
            key = instrument['canonical_round_key']
            if (parent['issuer_id'], key.casefold()) in round_keys:
                raise ValueError('Separat instrument har redan en projicerad canonical round')
            round_keys.add((parent['issuer_id'], key.casefold()))
            suffix = hashlib.sha256(key.encode()).hexdigest()[:16]
            card = {k: parent[k] for k in ('issuer_id', 'issuer_name', 'published_at', 'source_url')}
            card.update(id=f"transaction:{parent['issuer_id']}:{instrument['category']}:instrument:{suffix}",
                        category=instrument['category'], status=instrument['status'],
                        amount=None, amount_basis=None, currency=None, institutions=[],
                        source_document_ids=instrument['source_document_ids'],
                        history=[h for h in parent['history'] if h['document_id'] in instrument['source_document_ids']],
                        details={'financing_dossier': {}}, review_required=True, editorial_source='source',
                        signal_tone='yellow', signal_label='Källverifierat separat instrument',
                        projection_parent_id=parent['id'], projection_origin='audited_separate_instrument')
            facts = {k: v for k, v in instrument.items() if k != 'source_document_ids'}
            facts['announced_gross_amount'] = instrument['amount']
            correction_id = value_overrides[parent['id']]['field_correction_ids']['additional_instruments']
            _project_transaction_values(card, {'values': facts, 'correction_ids': [correction_id]})
            additions.append(card)
    if additions:
        child_overrides = effective_transaction_overrides(connection, [item['id'] for item in additions])
        for card in additions:
            patch = child_overrides.get(card['id'])
            if patch:
                _project_transaction_values(card, patch)
        items.extend(additions)
    _apply_bridge_loan_alerts(items)
    items.sort(key=lambda item: (_stamp(item["published_at"]), item["id"]), reverse=True)
    return items


def _apply_bridge_loan_alerts(items: list[dict[str, Any]]) -> None:
    """Flag bridge loans unless their source also announces an equity raise.

    A bridge loan is a liquidity event in its own right. It must not be
    presented as proof that a future equity raise will occur. The alert is
    cleared only by a current equity transaction citing the same public source
    document. This keeps the warning source-bound and leaves runway and
    screening calculations untouched.
    """
    equity_categories = {
        "rights_issue", "directed_issue", "unit_issue", "other_equity",
        "warrant_exercise",
    }
    equity_documents_by_issuer: dict[int, set[int]] = defaultdict(set)
    for item in items:
        if item.get("category") not in equity_categories:
            continue
        issuer_id = item.get("issuer_id")
        if not isinstance(issuer_id, int):
            continue
        equity_documents_by_issuer[issuer_id].update(
            int(document_id) for document_id in item.get("source_document_ids", [])
            if str(document_id).isdigit()
        )

    for item in items:
        if item.get("category") != "bridge_loan":
            continue
        issuer_id = item.get("issuer_id")
        bridge_documents = {
            int(document_id) for document_id in item.get("source_document_ids", [])
            if str(document_id).isdigit()
        }
        linked_documents = sorted(
            bridge_documents & equity_documents_by_issuer.get(issuer_id, set())
        )
        simultaneous_equity_announcement = bool(linked_documents)
        alert = {
            "severity": "info" if simultaneous_equity_announcement else "high",
            "equity_plan_announced_with_bridge": simultaneous_equity_announcement,
            "linked_equity_source_document_ids": linked_documents,
            "message": (
                "Brygglånet offentliggjordes tillsammans med ett emissionsbesked."
                if simultaneous_equity_announcement else
                "Brygglån utan samtidigt offentligt emissionsbesked i den källhistorik som hör till affären."
            ),
        }
        details = dict(item.get("details") or {})
        details["bridge_loan_alert"] = alert
        item["details"] = details
        if not simultaneous_equity_announcement:
            item["signal_tone"] = "red"
            item["signal_label"] = "Brygglån utan samtidigt emissionsbesked"


def _transaction_items(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Reuse the canonical source graph until the underlying SQLite store changes."""
    # A caller's existing read snapshot can predate the filesystem signature.
    # Publishing that snapshot under a newer WAL stamp poisons subsequent
    # reads. Snapshot-scoped callers must neither consume nor populate this
    # process-wide cache.
    if connection.in_transaction:
        return _build_transaction_items(connection)
    signature = _transaction_store_signature(connection)
    if signature is None:
        return _build_transaction_items(connection)
    key, version = signature
    with _TRANSACTION_CACHE_LOCK:
        cached = _TRANSACTION_CACHE.get(key)
        if cached is not None and cached[0] == version:
            return list(cached[1])
        items = _build_transaction_items(connection)
        _TRANSACTION_CACHE[key] = (version, items)
        if len(_TRANSACTION_CACHE) > 8:
            oldest = next(iter(_TRANSACTION_CACHE))
            if oldest != key:
                _TRANSACTION_CACHE.pop(oldest, None)
        return list(items)


def get_transaction_feed(
    connection: sqlite3.Connection, *, page: int = 1, page_size: int = 10,
    q: str = "", category: str = "", stage: str = "", advisor: str = "",
    agent: str = "", from_date: str = "",
) -> dict[str, Any]:
    """Return ten canonical transactions per page without side effects."""
    if type(page) is not int or page < 1 or type(page_size) is not int or page_size != 10:
        raise ValueError("page must be a positive integer and page_size must be 10")
    items = _transaction_items(connection)
    from .notification_groups import notifications
    if connection is not None and hasattr(connection,'execute') and connection.execute("SELECT 1 FROM sqlite_master WHERE name='financing_notification_members'").fetchone():
        items = notifications(connection,items)
    filters = (q, category, stage, advisor, agent, from_date)
    if any(not isinstance(value, str) or len(value) > 256 for value in filters):
        raise ValueError("invalid transaction filter")
    q, category, stage, advisor, agent, from_date = (value.strip() for value in filters)
    stages = {
        "announcement": {"announced", "proposed", "conditional"},
        "terms": {"subscription_period", "approved"},
        "outcome": {"preliminary_outcome", "completed"},
        "completed": {"registered"},
    }
    if category and category not in CATEGORY_LABELS:
        raise ValueError("invalid category")
    if stage and stage not in stages and stage not in STATUS_LABELS:
        raise ValueError("invalid stage")
    if from_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_date):
            raise ValueError("invalid date")
        datetime.strptime(from_date, "%Y-%m-%d")
    def institution_matches(item, name, role):
        return not name or any(p["role"] == role and name.casefold() in p["name"].casefold()
                               for p in item["institutions"])
    items = [item for item in items
             if (not q or q.casefold() in " ".join(str(item.get(k) or "") for k in
                 ("issuer_name", "headline", "summary")).casefold())
             and (not category or item["category"] == category)
             and (not stage or item["status"] in stages.get(stage, {stage}))
             and (not from_date or (item["published_at"] or "")[:10] >= from_date)
             and institution_matches(item, advisor, "Finansiell rådgivare")
             and institution_matches(item, agent, "Emissionsinstitut")]
    crm_statuses = {}
    if connection is not None and hasattr(connection, "execute"):
        try:
            rows = connection.execute(
                "SELECT issuer_id, COALESCE(status, 'missing') AS status FROM issuer_crm"
            ).fetchall()
            crm_statuses = {int(row["issuer_id"]): str(row["status"]) for row in rows}
        except sqlite3.OperationalError:
            pass
    items = [{
        **item,
        "crm_status": crm_statuses.get(
            int(item["issuer_id"]) if item.get("issuer_id") is not None else -1,
            "missing",
        ),
    } for item in items]
    total = len(items)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "page": page, "page_size": page_size,
            "total": total, "total_pages": (total + page_size - 1) // page_size,
            "has_next": start + page_size < total,
            "as_of": items[0]["published_at"] if items else None}


def get_transaction_detail(
    connection: sqlite3.Connection, transaction_id: str,
) -> dict[str, Any]:
    """Return one canonical transaction including its complete lifecycle."""
    transaction_id = str(transaction_id or "").strip()
    if not transaction_id.startswith("transaction:") or len(transaction_id) > 180:
        raise ValueError("invalid transaction id")
    try:
        issuer_id = int(transaction_id.split(":")[1])
    except (ValueError, IndexError):
        raise ValueError("invalid transaction id") from None
    for item in _build_transaction_items(connection, issuer_ids=[issuer_id]):
        if item["id"] == transaction_id:
            if connection is not None and hasattr(connection, "execute"):
                try:
                    row = connection.execute("SELECT COALESCE(status, 'missing') AS status FROM issuer_crm WHERE issuer_id=?", (item["issuer_id"],)).fetchone()
                    item = {**item, "crm_status": str(row["status"]) if row else "missing"}
                except sqlite3.OperationalError:
                    item = {**item, "crm_status": "missing"}
            else:
                item = {**item, "crm_status": "missing"}
            return _with_market_reaction(connection, item)
    raise KeyError(transaction_id)


def _with_market_reaction(
    connection: sqlite3.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    """Attach only market data evidenced by this transaction's source documents."""
    document_ids = [int(value) for value in item.get("source_document_ids", []) if str(value).isdigit()]
    if not document_ids:
        return item
    placeholders = ",".join("?" for _ in document_ids)
    row = connection.execute(
        f"""SELECT metrics.financing_event_id, metrics.outcome_date, metrics.symbol,
                   metrics.close_average_5d, metrics.volume_weighted_close_5d,
                   metrics.total_volume_5d, metrics.source, metrics.source_url,
                   metrics.complete
            FROM financing_outcome_market_metrics metrics
            JOIN financing_events event ON event.id=metrics.financing_event_id
            JOIN financing_event_evidence evidence
              ON evidence.financing_event_id=metrics.financing_event_id
            WHERE event.issuer_id=? AND evidence.document_id IN ({placeholders})
            ORDER BY metrics.complete DESC, metrics.retrieved_at DESC
            LIMIT 1""",
        (item.get("issuer_id"), *document_ids),
    ).fetchone()
    if row is None:
        return item
    days = connection.execute(
        """SELECT trading_date AS date, close, volume
           FROM financing_outcome_market_days
           WHERE financing_event_id=? ORDER BY trading_date""",
        (row["financing_event_id"],),
    ).fetchall()
    result = dict(item)
    details = dict(result.get("details") or {})
    details["market_reaction"] = {
        "outcome_date": row["outcome_date"],
        "symbol": row["symbol"],
        "average_close_5d": row["close_average_5d"],
        "volume_weighted_average_close_5d": row["volume_weighted_close_5d"],
        "total_volume_5d": row["total_volume_5d"],
        "complete": bool(row["complete"]),
        "source": row["source"],
        "source_url": row["source_url"],
        "daily_bars": [dict(day) for day in days],
    }
    result["details"] = details
    return result
