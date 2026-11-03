"""Tester för P0-2: kassaflödessektionen får garantierad prioritet i sidurvalet.

Verifierar att sidor med kassaflödesmarkörer alltid inkluderas — även när de
ligger långt in i en stor årsredovisning (sid 45-78) och skulle ha trunkerats
bort av max_report_pages eller relevansrankingen. Täcker svenska, engelska
och danska/norska standardformat.
"""

from __future__ import annotations

from capital_radar.text_selection import select_relevant_text


def _build_report(*, cashflow_page: int, total_pages: int, page_size: int = 400) -> str:
    """Bygg en syntetisk rapport där kassaflödessektionen ligger på cashflow_page."""
    pages = []
    for number in range(1, total_pages + 1):
        if number == cashflow_page:
            content = (
                "Koncernens kassaflödesanalys. "
                "Kassaflöde från den löpande verksamheten -30 533. "
                "Cash flow from operating activities. "
                "Finansieringsverksamheten. "
            )
            # Fyll ut sidan till ungefär page_size tecken.
            content = (content + "detaljer " * 20)[:page_size]
        else:
            content = ("vanlig berättande text utan siffror " * 20)[:page_size]
        pages.append(f"[PAGE {number}]\n{content}")
    return "\n\n".join(pages)


def test_cashflow_page_deep_in_report_is_included():
    # Kassaflöde på sidan 50 i en 88-sidig årsredovisning (Pricer-fallet).
    text = _build_report(cashflow_page=50, total_pages=88)
    selected = select_relevant_text(
        text,
        max_chars=120000,
        document_title="Årsredovisning 2025",
        max_report_pages=10,
    )
    assert 50 in selected.selected_pages


def test_cashflow_pages_exceed_max_report_pages_but_still_included():
    # Flera kassaflödessidor (tabell bruten över sidgräns) ska alla med,
    # även om de tillsammans överstiger max_report_pages.
    pages = []
    for number in range(1, 30):
        if 20 <= number <= 26:
            content = "Kassaflöde från den löpande verksamheten. " + ("data " * 60)
        else:
            content = "annan text " * 60
        pages.append(f"[PAGE {number}]\n{content}")
    text = "\n\n".join(pages)
    selected = select_relevant_text(
        text,
        max_chars=120000,
        document_title="Delårsrapport",
        max_report_pages=5,
    )
    # Alla kassaflödessidor (20-26) ska vara med trots att max_report_pages=5.
    for number in range(20, 27):
        assert number in selected.selected_pages, f"sida {number} saknas"


def test_english_cash_flow_markers_included():
    pages = []
    for number in range(1, 20):
        if number == 12:
            content = (
                "Consolidated statement of cash flows. "
                "Cash flow from operating activities (1,008). "
                "Financing activities. "
            ) + ("figures " * 50)
        else:
            content = "narrative " * 60
        pages.append(f"[PAGE {number}]\n{content}")
    text = "\n\n".join(pages)
    selected = select_relevant_text(
        text,
        max_chars=120000,
        document_title="Annual report",
        max_report_pages=5,
    )
    assert 12 in selected.selected_pages


def test_danish_cash_flow_markers_included():
    # Loyal Solutions-fallet: dansk årsrapport med pengestrøm.
    pages = []
    for number in range(1, 20):
        if number == 15:
            content = (
                "Pengestrømsopgørelse. Pengestrøm fra driftsaktiviteter (2.920). "
                "Likviditetsanalyse. "
            ) + ("tal " * 60)
        else:
            content = "tekst " * 60
        pages.append(f"[PAGE {number}]\n{content}")
    text = "\n\n".join(pages)
    selected = select_relevant_text(
        text,
        max_chars=120000,
        document_title="Årsrapport",
        max_report_pages=5,
    )
    assert 15 in selected.selected_pages


def test_cashflow_page_prioritized_over_early_narrative_pages():
    # Kassaflödessidan ska prioriteras även om den ligger efter många
    # tidiga sidor som annars skulle fylla max_report_pages-kvoten.
    text = _build_report(cashflow_page=45, total_pages=78)
    selected = select_relevant_text(
        text,
        max_chars=120000,
        document_title="Årsredovisning",
        max_report_pages=10,
    )
    assert 45 in selected.selected_pages
