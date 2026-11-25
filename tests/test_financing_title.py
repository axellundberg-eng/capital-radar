"""Pressmeddelandets brödtext börjar vid rubriken, inte vid MFN:s meny."""

from radar.financing import build_message, from_title


def test_text_starts_at_the_title_followed_by_the_publication_time():
    text = ("Meny\nRubrik X\nKalender " + "k" * 6000 + "\nRubrik X\n2026-11-25 07:50:00\n"
            "Garantiersättningen uppgår till 12 procent\n" + "b" * 3000 + "\nRubrik X\nKälla MFN\nSidfot")
    assert from_title("Rubrik X", text).startswith("Rubrik X\n2026-11-25")
    assert "12 procent" in build_message("Rubrik X", "2026-11-25", text)


def test_missing_title_keeps_the_text():
    assert from_title("Saknas", "abc") == "abc"
    assert from_title("Rubrik X", "Rubrik X\nKälla MFN") == "Rubrik X\nKälla MFN"
