"""Mindre slöseri per AI-anrop: kortare nyhetstext som behåller rådgivare, inga anrop för informations-
meddelanden och ett kompaktare svarsformat vid rapportläsning (samma data)."""

from __future__ import annotations

import json

from radar import news
from radar.extract import parse_response
from radar.financing import HEAD_CHARS, TAIL_CHARS, trim_text


def test_trimmed_news_text_keeps_terms_at_the_top_and_advisers_at_the_end():
    filler = "Bolaget utvecklar produkter för den globala marknaden. " * 400          # ~22 000 tecken
    text = ("Styrelsen beslutar om en företrädesemission om 40 MSEK.\n" + filler + "\n"
            "Rådgivare: Corpura Fondkommission AB är finansiell rådgivare och Advokatfirman X är legal rådgivare. "
            "Nordic Issuing AB agerar emissionsinstitut.\n" + "Viktig information. " * 50)
    trimmed = trim_text(text)
    assert trimmed.startswith("Styrelsen beslutar om en företrädesemission om 40 MSEK.")
    assert "Corpura Fondkommission AB är finansiell rådgivare" in trimmed and "agerar emissionsinstitut" in trimmed
    assert len(trimmed) <= HEAD_CHARS + TAIL_CHARS + 10 < len(text)
    assert trim_text("Kort text.") == "Kort text."


def test_informational_steps_of_a_known_deal_are_recognised():
    for title in ("Sista dag för handel med uniträtter i Bolagets företrädesemission",
                  "Teckningsperioden i Bolagets företrädesemission inleds idag",
                  "The subscription period in the rights issue begins today",
                  "Nytt antal aktier och röster i Bolaget AB", "Bolaget offentliggör informationsmemorandum"):
        assert news.is_info(title), title
    for title in ("Bolaget offentliggör utfall i företrädesemissionen", "Bolaget genomför en riktad nyemission om 20 MSEK",
                  "Bolaget tecknar låneavtal om 10 MSEK"):
        assert not news.is_info(title), title


def test_report_answer_with_one_row_per_kind_gives_the_same_facts_as_one_row_per_column():
    base = {"report_period_end": "2026-09-30", "unit": "TSEK", "currency": "SEK",
            "cash": {"value": 22500, "date": "2026-09-30", "page": 4, "quote": "Likvida medel 22 500"}}
    grouped = {**base, "cash_flows": [{"kind": "operating_cash_flow", "page": 9, "quote": "Kassaflöde -8 369 -24 996",
                                       "columns": [{"period_start": "2026-07-01", "period_end": "2026-09-30", "value": -8369},
                                                   {"period_start": "2026-01-01", "period_end": "2026-09-30", "value": -24996}]}]}
    legacy = {**base, "cash_flows": [
        {"kind": "operating_cash_flow", "value": -8369, "period_start": "2026-07-01", "period_end": "2026-09-30",
         "page": 9, "quote": "Kassaflöde -8 369 -24 996"},
        {"kind": "operating_cash_flow", "value": -24996, "period_start": "2026-01-01", "period_end": "2026-09-30",
         "page": 9, "quote": "Kassaflöde -8 369 -24 996"}]}
    a, b = parse_response(json.dumps(grouped)), parse_response(json.dumps(legacy))
    assert a.facts == b.facts and len(a.facts) == 3
    assert {f.value for f in a.facts if f.kind == "operating_cash_flow"} == {-8_369_000, -24_996_000}
