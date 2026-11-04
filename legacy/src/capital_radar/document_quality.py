from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentQuality:
    score: int
    status: str
    reason: str
    signals: tuple[str, ...]


_SIGNALS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "cash",
        45,
        (
            r"\bcash and cash equivalents\b",
            r"\bcash equivalents\b",
            r"\bcash balance\b",
            r"\bcash and bank balances?\b",
            r"\blikvida medel\b",
            r"\bkassa och bank\b",
        ),
    ),
    (
        "operating_cash_flow",
        45,
        (
            r"\bcash flows? from operating activities\b",
            r"\bnet cash (?:flow )?from operating activities\b",
            r"\bcash flows? from operations\b",
            r"\boperating cash flow\b",
            r"\bkassaflöde från den löpande verksamheten\b",
            r"\bkassaflöde från löpande verksamhet(?:en)?\b",
            r"\bkassaflöde från den[\s\S]{0,500}"
            r"\blöpande verksamheten\b",
            r"\brapport över kassaflöden[\s\S]{0,1500}"
            r"\bden löpande verksamheten\b",
            r"\boperativt kassaflöde\b",
        ),
    ),
    (
        "balance_sheet",
        8,
        (
            r"\bbalance sheet\b",
            r"\bstatement of financial position\b",
            r"\bbalansräkning\b",
            r"\brapport över finansiell ställning\b",
        ),
    ),
    (
        "report_period",
        5,
        (
            r"\b(?:q[1-4]|quarter|interim report|delårsrapport)\b",
            r"\b20\d{2}[-–]\d{2}[-–]\d{2}\b",
            r"\bjanuary\b|\bfebruary\b|\bmarch\b|\bapril\b",
            r"\bjanuari\b|\bfebruari\b|\bmars\b|\bapril\b",
        ),
    ),
    (
        "subsequent_events",
        4,
        (
            r"\bsubsequent events\b",
            r"\bevents after the reporting period\b",
            r"\bhändelser efter rapportperiod",
        ),
    ),
)


def _squash_pattern(pattern: str) -> str:
    """Gor ett monster whitespace-tolerant.

    pypdf:s layout-lage bryter svenska nyckelfraser med extra mellanslag
    ("Lik vida medel") sa att vanliga \b-monster missar. Genom att kora
    monstret mot en whitespace-kollapsad text (utan \b) traffar vi aven
    sonderbrutna varianter. Anvands som komplement, aldrig istallet.
    """
    # Ta bort \b-ankare (meningslosa utan mellanslag) och alla literala
    # mellanslag/\s eftersom maltexten ar helt kollapsad.
    out = pattern.replace(r"\b", "")
    out = re.sub(r"\\s\+|\\s\*|\s+", "", out)
    return out


def evaluate_document_quality(
    text: str,
    *,
    profile_category: str,
    page_count: int | None,
) -> DocumentQuality:
    if profile_category != "report":
        return DocumentQuality(0, "not_required", "Ej finansiell rapport", ())
    haystack = text.casefold()
    # Kompletterande kollapsad text for att fanga layout-sonderbrutna fraser.
    squashed = re.sub(r"\s+", "", haystack)
    signals: list[str] = []
    score = 0
    for name, weight, patterns in _SIGNALS:
        hit = any(
            re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns
        )
        if not hit:
            hit = any(
                re.search(_squash_pattern(pattern), squashed, re.IGNORECASE)
                for pattern in patterns
            )
        if hit:
            signals.append(name)
            score += weight
    score += min(max(int(page_count or 0) - 3, 0), 8)
    required = {"cash", "operating_cash_flow"}
    missing = sorted(required.difference(signals))
    if missing:
        labels = {
            "cash": "kassa och likvida medel",
            "operating_cash_flow": "operativt kassaflöde",
        }
        reason = "Saknar " + " och ".join(labels[name] for name in missing)
        return DocumentQuality(score, "incomplete", reason, tuple(signals))
    return DocumentQuality(
        score,
        "complete",
        "Innehåller kassa och operativt kassaflöde",
        tuple(signals),
    )
