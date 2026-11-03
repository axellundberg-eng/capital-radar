from __future__ import annotations

import re
from dataclasses import dataclass


_PAGE = re.compile(r"(?m)^\[PAGE (\d+)\]\s*$")

# Markörer för kassaflödessektionen. Sidor som innehåller någon av dessa får
# GARANTIERAD prioritet i urvalet — de får aldrig trunkeras bort av
# max_report_pages eller max_chars. Detta är P0-2: balansräkning och
# kassaflödesanalys (ofta sid 45-78 i en årsredovisning) måste alltid nå
# modellen, annars blir operating_cash_flow null. Täcker svenska, engelska
# och danska/norska standardformat samt brutna kolumnrubriker.
_CASHFLOW_MARKERS: tuple[str, ...] = (
    "kassaflöde",
    "cash flow",
    "cashflow",
    "kassaflöden",
    "pengestrøm",          # dansk/norsk
    "pengeström",          # norsk bokmål-variant
    "likviditetsanalyse",  # dansk
    "finansieringsverksamheten",
    "financing activities",
    "löpande verksamheten",
    "operating activities",
    "driftsaktiviteter",   # dansk/norsk
)

_TERMS: tuple[tuple[str, int], ...] = (
    ("cash and cash equivalents", 12),
    ("likvida medel", 12),
    ("cash flow from operating activities", 12),
    ("kassaflöde från den löpande verksamheten", 12),
    ("operating cash flow", 10),
    ("financing activities", 8),
    ("finansieringsverksamheten", 8),
    ("subsequent events", 10),
    ("händelser efter rapportperiod", 10),
    ("events after the reporting period", 10),
    ("going concern", 12),
    ("fortsatt drift", 12),
    ("rights issue", 10),
    ("företrädesemission", 10),
    ("directed issue", 10),
    ("riktad emission", 10),
    ("credit facility", 9),
    ("kreditfacilitet", 9),
    ("loan agreement", 8),
    ("låneavtal", 8),
    ("bridge loan", 10),
    ("brygglån", 10),
    ("balance sheet", 7),
    ("balansräkning", 7),
    ("financial position", 7),
    ("finansiell ställning", 7),
    ("capitalised development", 5),
    ("aktiverade utvecklingsutgifter", 5),
)


@dataclass(frozen=True)
class TextSelection:
    text: str
    selected_pages: tuple[int, ...]
    original_char_count: int
    selected_char_count: int
    estimated_tokens: int


def split_pages(text: str) -> list[tuple[int, str]]:
    matches = list(_PAGE.finditer(text))
    if not matches:
        return [(1, text.strip())]
    pages: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group(1)), text[match.end() : end].strip()))
    return pages


def select_relevant_text(
    text: str,
    *,
    max_chars: int,
    document_title: str = "",
    max_report_pages: int = 12,
) -> TextSelection:
    pages = split_pages(text)
    title = document_title.casefold()
    event_document = any(
        term in title
        for term in (
            "emission", "issue", "loan", "lån", "facility", "financing",
            "finansiering", "acquisition", "förvärv", "divest", "avyttr",
        )
    )
    if len(text) <= max_chars and (
        event_document or len(pages) <= max_report_pages
    ):
        selected = tuple(number for number, _ in pages)
        return TextSelection(
            text, selected, len(text), len(text), _estimate_tokens(text)
        )

    scores: dict[int, int] = {}
    for number, page_text in pages:
        haystack = page_text.casefold()
        scores[number] = sum(
            weight * haystack.count(term) for term, weight in _TERMS
        )
        if number <= 3:
            scores[number] += 20 - number

    by_number = dict(pages)

    # P0-2: Identifiera sidor som innehåller kassaflödessektionen. Dessa får
    # garantierad prioritet och räknas INTE mot max_report_pages — de måste
    # alltid nå modellen. Vi inkluderar även grannsidor (±1) eftersom en
    # kassaflödestabell ofta bryts över en sidgräns.
    cashflow_pages: set[int] = set()
    for number, page_text in pages:
        haystack = page_text.casefold()
        if any(marker in haystack for marker in _CASHFLOW_MARKERS):
            cashflow_pages.add(number)
            if number - 1 in by_number:
                cashflow_pages.add(number - 1)
            if number + 1 in by_number:
                cashflow_pages.add(number + 1)

    ranked = (
        [number for number, _ in pages]
        if event_document
        else sorted(scores, key=lambda number: (-scores[number], number))
    )
    priority: list[int] = [number for number, _ in pages[: min(3, len(pages))]]
    for number in ranked:
        candidates = (number,) if event_document else (number, number - 1, number + 1)
        for candidate in candidates:
            if candidate in by_number and candidate not in priority:
                priority.append(candidate)

    selected: set[int] = set()
    current_length = 0

    def _try_add(number: int) -> bool:
        nonlocal current_length
        chunk = f"[PAGE {number}]\n{by_number[number]}".strip()
        separator = 2 if selected else 0
        if current_length + separator + len(chunk) > max_chars:
            return False
        selected.add(number)
        current_length += separator + len(chunk)
        return True

    # Steg 1: Lägg ALLTID in kassaflödessidor först (i sidordning). Dessa
    # räknas inte mot max_report_pages. Om en enskild sida är för stor för
    # max_chars hoppas den över men resterande kassaflödessidor prövas.
    for number in sorted(cashflow_pages):
        if number in by_number and number not in selected:
            _try_add(number)

    # Steg 2: Fyll på med övriga prioriterade sidor under max_report_pages.
    # Kassaflödessidor som redan lagts till räknas inte mot sidtaket.
    non_cashflow_selected = 0
    for number in priority:
        if number in selected:
            continue
        if not event_document and non_cashflow_selected >= max_report_pages:
            break
        if _try_add(number):
            if number not in cashflow_pages:
                non_cashflow_selected += 1

    selected_pages = sorted(selected)
    chunks = [
        f"[PAGE {number}]\n{by_number[number]}".strip()
        for number in selected_pages
    ]
    selected_text = "\n\n".join(chunks)
    return TextSelection(
        selected_text,
        tuple(selected_pages),
        len(text),
        len(selected_text),
        _estimate_tokens(selected_text),
    )


def _estimate_tokens(text: str) -> int:
    # Conservative language-agnostic estimate for diagnostics.
    return max(1, (len(text) + 2) // 3)
