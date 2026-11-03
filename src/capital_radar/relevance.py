from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RelevanceResult:
    score: int
    reason: str
    queued_for_analysis: bool
    profile_category: str


_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "rapport",
        100,
        (
            r"\bdelårsrapport(?:en)?\b",
            r"\bhalvårsrapport(?:en)?\b",
            r"\bkvartalsrapport(?:en)?\b",
            r"\bbokslutskommunik[eé]n?\b",
            r"\bårsredovisning(?:en)?\b",
            r"\brapport(?:en)?\s+(?:för|från)\s+(?:det\s+)?(?:första|andra|tredje|fjärde)\s+kvartalet\b",
            r"\bq[1-4]\s*20\d{2}\b",
            r"\b(?:q[1-4]|h[1-2])\s*(?:20\d{2}\s*)?(?:interim\s*)?(?:rapport|report|results)\b",
            r"\bquarterly report\b",
            r"\binterim report\b",
            r"\bhalf[- ]year report\b",
            r"\breport for the (?:first|second|third|fourth) quarter\b",
            r"\byear[- ]end report\b",
            r"\bannual report\b",
        ),
    ),
    (
        "eget kapital",
        100,
        (
            r"\bnyemission\b",
            r"\bföreträdesemission\b",
            r"\briktad emission\b",
            r"\bprivate placement\b",
            r"\brights issue\b",
            r"\bshare issue\b",
            r"\bteckningsoption",
            r"\bwarrant exercise\b",
            r"\bunit emission\b",
        ),
    ),
    (
        "skuld och finansiering",
        100,
        (
            r"\blåneavtal\b",
            r"\blånelöfte\b",
            r"\bkreditfacilitet\b",
            r"\bbrygglån\b",
            r"\bkonvertibel",
            r"\brefinansier",
            r"\bfinansiering\b",
            r"\bfinancing\b",
            r"\bcredit facilit",
            r"\bloan agreements?\b",
            r"\bbridge loans?\b",
            r"\bconvertible",
            r"\bgoing concern\b",
            r"\blikviditet\b",
            r"\bliquidity\b",
        ),
    ),
    (
        "transaktion",
        75,
        (
            r"\bförvärv",
            r"\bavyttring",
            r"\bfusion",
            r"\bearn[- ]out\b",
            r"\bacquisition\b",
            r"\bdivest",
            r"\bmerger\b",
            r"\bcash consideration\b",
            r"\basset sale\b",
        ),
    ),
    (
        "annan möjlig kassapåverkan",
        55,
        (
            r"\bbidrag\b",
            r"\bgrant\b",
            r"\bmilstolpsbetalning\b",
            r"\bmilestone payment\b",
            r"\bupfront payment\b",
            r"\butdelning\b",
            r"\bdividend\b",
            r"\bkapitalstruktur\b",
            r"\bcapital structure\b",
        ),
    ),
)

_NON_EVENT_PATTERNS = (
    r"\b(?:invitation|inbjudan)\b.*\b(?:webinar|webcast|presentation|webbsändning)\b",
    r"\b(?:webinar|webcast|live presentation)\b",
    r"\binvitation to (?:the )?(?:presentation|webcast).*(?:report|results)\b",
    r"\binbjudan till (?:presentation|webbsändning).*(?:rapport|resultat)\b",
    r"\bchanges? (?:the )?publication date\b",
    r"\bändrar publiceringsdatum\b",
    r"\bfinancial calendar\b",
    r"\bfinansiell kalender\b",
)

_PROFILE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "profit_warning",
        (
            r"\bvinstvarn",
            r"\bresultatvarn",
            r"\bomvänd vinstvarn",
            r"\bprofit warning\b",
            r"\bearnings warning\b",
        ),
    ),
    (
        "report",
        (
            r"\bdelårsrapport(?:en)?\b",
            r"\bhalvårsrapport(?:en)?\b",
            r"\bkvartalsrapport(?:en)?\b",
            r"\bbokslutskommunik[eé]n?\b",
            r"\bårsredovisning(?:en)?\b",
            r"\brapport(?:en)?\s+(?:för|från)\s+(?:det\s+)?(?:första|andra|tredje|fjärde)\s+kvartalet\b",
            r"\bq[1-4]\s*20\d{2}\b",
            r"\bquarterly report\b",
            r"\binterim report\b",
            r"\b(?:q[1-4]|h[1-2])\s*(?:20\d{2}\s*)?(?:interim\s*)?(?:rapport|report|results)\b",
            r"\bhalf[- ]year report\b",
            r"\byear[- ]end report\b",
            r"\bannual report\b",
            r"\breport for the (?:first|second|third|fourth) quarter\b",
            # Täck rapporttitlar som tidigare föll igenom klassificeringen
            # (profile_category='other') trots att dokumentet är en finansiell
            # rapport. Utan 'report'-klassning får extraktionen ingen
            # classification_hint och runway kan aldrig beräknas.
            r"\b(?:first|second|third|fourth)\s+quarter\s+(?:report|results)\b",
            r"\b(?:q[1-4]|kvartal\s*[1-4])\s+(?:rapport|report|results)\b",
            r"\bbokslut(?:et)?\b",
            r"\b(?:preliminärt?\s+)?bokslut\s+20\d{2}\b",
            r"\bresultaträkning(?:en)?\b",
            r"\bbalansräkning(?:en)?\b",
            r"\bkoncernredovisning(?:en)?\b",
            r"\bfinansiell\s+rapport\b",
            r"\bperiodrapport(?:en)?\b",
            r"\brapport\s+(?:januari|februari|mars|april|maj|juni|juli|augusti|september|oktober|november|december)\b",
        ),
    ),
    (
        "financing",
        tuple(
            pattern
            for reason, _score, patterns in _RULES
            if reason in {"eget kapital", "skuld och finansiering"}
            for pattern in patterns
        ),
    ),
    (
        "transaction",
        tuple(
            pattern
            for reason, _score, patterns in _RULES
            if reason == "transaktion"
            for pattern in patterns
        ),
    ),
    (
        "commercial_deal",
        (
            r"\btecknar (?:ett )?(?:avtal|samarbetsavtal)\b",
            r"\bingår (?:ett )?(?:avtal|samarbete)\b",
            r"\bavtal med\b",
            r"\bny(?:tt|a)? (?:avtal|order|kund|samarbete)\b",
            r"\border (?:om|värd|från)\b",
            r"\bkundorder\b",
            r"\bkontrakt\b",
            r"\bsamarbetsavtal\b",
            r"\bsigns? (?:an? )?agreement\b",
            r"\bagreement with\b",
            r"\bnew (?:agreement|contract|order|customer|partnership)\b",
            r"\breceives? (?:an? )?order\b",
            r"\bpartnership\b",
            r"\bcollaboration\b",
            r"\bselected by\b",
        ),
    ),
    (
        "corporate",
        (
            r"\bbolagsstämma\b",
            r"\bgeneral meeting\b",
            r"\bstyrelse\b",
            r"\bboard of directors\b",
            r"\bvd\b",
            r"\bceo\b",
            r"\bcfo\b",
            r"\bcertified adviser\b",
        ),
    ),
)


def classify_title(title: str, category: str | None = None) -> RelevanceResult:
    haystack = f"{title} {category or ''}".casefold()
    profile_category = _profile_category(haystack)
    if any(
        re.search(pattern, haystack, flags=re.IGNORECASE)
        for pattern in _NON_EVENT_PATTERNS
    ):
        return RelevanceResult(
            0,
            "kalender/inbjudan utan ny kassahändelse",
            False,
            "calendar",
        )
    best_score = 0
    reasons: list[str] = []
    for reason, score, patterns in _RULES:
        if any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in patterns):
            best_score = max(best_score, score)
            reasons.append(reason)
    if not reasons:
        return RelevanceResult(
            0,
            "ingen kassasignal i metadata",
            False,
            profile_category,
        )
    return RelevanceResult(
        best_score,
        ", ".join(dict.fromkeys(reasons)),
        best_score >= 55,
        profile_category,
    )


def _profile_category(haystack: str) -> str:
    for category, patterns in _PROFILE_RULES:
        if any(
            re.search(pattern, haystack, flags=re.IGNORECASE)
            for pattern in patterns
        ):
            return category
    return "other"
