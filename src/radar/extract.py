"""Strukturerad extraktion av kassa och kassaflöden med DeepSeek (JSON)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .units import parse_unit

KINDS = ("operating_cash_flow", "capex", "operating_investments", "investing_cash_flow")
KEYWORDS = (
    "kassaflöde", "kassaflödesanalys", "likvida medel", "den löpande verksamheten",
    "investeringsverksamheten", "cash flow", "cash and cash equivalents",
    "operating activities", "investing activities", "liquid funds", "balansräkning",
    "summa tillgångar", "total assets", "balance sheet", "kassa och bank",
)
BALANCE_HINTS = ("balansräkning", "summa tillgångar", "balance sheet", "total assets")
MAX_PAGES = 8
MAX_PAGE_CHARS = 6000

PROMPT = """Du läser en kvartals- eller årsrapport från ett börsnoterat bolag.
Extrahera siffror för INNEVARANDE period. Använd aldrig jämförelsekolumner från föregående år.

Svara med enbart ett JSON-objekt:
{
  "report_period_start": "YYYY-MM-DD",
  "report_period_end": "YYYY-MM-DD",
  "unit": "enheten som tabellerna anges i, t.ex. TSEK, MSEK, kSEK, SEK, TEUR",
  "currency": "SEK",
  "cash": {"value": tal, "date": "YYYY-MM-DD", "page": sidnummer, "quote": "kort citat"} eller null,
  "short_term_investments": {"value": tal, "date": "YYYY-MM-DD", "page": sidnummer, "quote": "kort citat"} eller null,
  "cash_flows": [
    {"kind": "operating_cash_flow | capex | operating_investments | investing_cash_flow",
     "value": tal, "period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD",
     "page": sidnummer, "quote": "kort citat"}
  ]
}

Regler:
- "value" anges exakt som i tabellen, i angiven enhet, med tecken (utflöde negativt).
- cash = likvida medel vid periodens slut.
- short_term_investments = kortfristiga placeringar (t.ex. räntefonder, kortfristiga
  finansiella placeringar) i balansräkningen vid periodens slut, om de inte redan ingår
  i likvida medel. Annars null.
- operating_cash_flow = kassaflöde från den löpande verksamheten (efter förändring av rörelsekapital).
- capex = investeringar i materiella och immateriella anläggningstillgångar (inklusive
  aktiverade utvecklingsutgifter), summerat om de står på flera rader. Inte förvärv av bolag.
- operating_investments = kassaflödet från investeringsverksamheten MINUS förvärv och
  avyttringar av dotterbolag/rörelser, finansiella placeringar och andelar i intresse-
  eller joint venture-bolag. Försäljning av inventarier räknas med (netto). Ange det
  beräknade talet och citera raderna du använt.
- investing_cash_flow = kassaflöde från investeringsverksamheten totalt.
- Ta med varje periodkolumn för innevarande år som finns (t.ex. både kvartalet och hittills i år).
- Hoppa över en siffra hellre än att gissa.
"""


class ChatClient(Protocol):
    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]: ...


class DeepSeekClient:
    def __init__(self, api_key: str, *, model: str, base_url: str, timeout: float = 120.0) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)

    def complete(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        response = self._client.chat.completions.create(
            model=self.model, temperature=0, max_tokens=8000,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        usage = response.usage
        choice = response.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            raise ExtractionError(f"tomt svar (finish_reason={choice.finish_reason})")
        return content, {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }


@dataclass(frozen=True)
class ExtractedFact:
    kind: str
    value: float
    currency: str | None
    period_start: str | None
    period_end: str | None
    page: int | None
    quote: str | None


@dataclass(frozen=True)
class Extraction:
    period_start: str | None
    period_end: str
    currency: str | None
    facts: tuple[ExtractedFact, ...]


class ExtractionError(ValueError):
    pass


def select_pages(page_texts: list[str]) -> list[int]:
    """Sidor (1-indexerade) med flest träffar på kassaflödes- och kassaord."""
    scored = []
    for number, text in enumerate(page_texts, start=1):
        lower = text.lower()
        score = sum(lower.count(word) for word in KEYWORDS)
        if score:
            scored.append((score, number))
    chosen = [number for _, number in sorted(scored, reverse=True)[:MAX_PAGES]]
    # Balansräkningen ska alltid med: annars saknas kassan trots att den finns.
    balance = next((n for n, t in enumerate(page_texts, start=1)
                    if any(h in t.lower() for h in BALANCE_HINTS)), None)
    if balance and balance not in chosen:
        chosen = chosen[:MAX_PAGES - 1] + [balance]
    best = sorted(chosen)
    return best or list(range(1, min(len(page_texts), MAX_PAGES) + 1))


def build_user_message(page_texts: list[str], pages: list[int]) -> str:
    parts = [f"--- Sida {n} ---\n{page_texts[n - 1][:MAX_PAGE_CHARS]}" for n in pages]
    return "Rapportens sidor:\n\n" + "\n\n".join(parts)


def parse_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as error:
        raise ExtractionError(f"ogiltigt datum: {value!r}") from error


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[\s ]", "", str(value)).replace("−", "-").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def json_object(raw: str) -> dict:
    """Plocka ut JSON-objektet ur ett svar (tål kodstaket och text runt)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end_brace = text.find("{"), text.rfind("}")
    if start > 0 or (end_brace != -1 and end_brace < len(text) - 1):
        text = text[start:end_brace + 1] if start != -1 and end_brace > start else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"svaret är inte JSON ({len(raw)} tecken)") from error
    if not isinstance(data, dict):
        raise ExtractionError("svaret är inte ett objekt")
    return data


def parse_response(raw: str) -> Extraction:
    data = json_object(raw)
    end = parse_date(data.get("report_period_end"))
    if not end:
        raise ExtractionError("rapportperiodens slut saknas")
    unit = parse_unit(data.get("unit"), data.get("currency"))
    if unit is None:
        raise ExtractionError("enhet och valuta saknas")
    multiplier, currency = unit
    facts = []
    cash = data.get("cash")
    if isinstance(cash, dict) and parse_number(cash.get("value")) is not None:
        facts.append(ExtractedFact("cash", parse_number(cash["value"]) * multiplier, currency, None,
                                   parse_date(cash.get("date")) or end, cash.get("page"), cash.get("quote")))
    placements = data.get("short_term_investments")
    if isinstance(placements, dict) and parse_number(placements.get("value")) is not None:
        facts.append(ExtractedFact("short_term_investments", abs(parse_number(placements["value"])) * multiplier,
                                   currency, None, parse_date(placements.get("date")) or end,
                                   placements.get("page"), placements.get("quote")))
    for item in data.get("cash_flows") or []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        value = parse_number(item.get("value"))
        if value is None:
            continue
        facts.append(ExtractedFact(item["kind"], value * multiplier, currency, parse_date(item.get("period_start")),
                                   parse_date(item.get("period_end")) or end, item.get("page"), item.get("quote")))
    return Extraction(parse_date(data.get("report_period_start")), end, currency, tuple(facts))


def extract(client: ChatClient, page_texts: list[str], *, attempts: int = 2
            ) -> tuple[Extraction, list[int], str, dict[str, int]]:
    """Ett nytt försök vid tomt eller trasigt svar; sedan fel med svaret bifogat."""
    pages = select_pages(page_texts)
    message = build_user_message(page_texts, pages)
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    last_error: ExtractionError | None = None
    raw = ""
    for _ in range(max(1, attempts)):
        try:
            raw, usage = client.complete(PROMPT, message)
            for key in usage_total:
                usage_total[key] += usage.get(key, 0)
            return parse_response(raw), pages, raw, usage_total
        except ExtractionError as error:
            last_error = error
    last_error.raw = raw  # type: ignore[attr-defined]
    raise last_error
