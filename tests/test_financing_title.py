"""Pressmeddelandets brödtext börjar vid rubriken, inte vid MFN:s meny."""

from radar.financing import build_message, from_title


def test_text_starts_at_the_last_title_occurrence():
    text = "Meny\nRubrik X\nKalender " + "k" * 6000 + "\nRubrik X\nGarantiersättningen uppgår till 12 procent"
    assert from_title("Rubrik X", text).startswith("Rubrik X\nGaranti")
    assert "12 procent" in build_message("Rubrik X", "2026-11-25", text)


def test_missing_title_keeps_the_text():
    assert from_title("Saknas", "abc") == "abc"
