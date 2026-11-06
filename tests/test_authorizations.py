"""Tester för emissionsbemyndiganden ur stämmokommunikéer (regex, ingen AI, inget nätverk)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from radar import authorizations, mfn
from radar.db import connect
from radar.settings import Settings

SV = ("Kommuniké från årsstämma i Foo AB. Foo AB höll årsstämma den 12 maj 2026. Stämman beslutade om "
      "bemyndigande för styrelsen att, vid ett eller flera tillfällen före nästa årsstämma, med eller utan "
      "avvikelse från aktieägarnas företrädesrätt, besluta om nyemission av aktier, teckningsoptioner och/eller "
      "konvertibler. Bemyndigandet ska vara begränsat till högst 20 procent av antalet aktier i bolaget.")
EN = ("Bulletin from the Annual General Meeting. The AGM resolved to authorize the board of directors to resolve "
      "on the issue of new shares, up to 5,000,000 shares, in one or several occasions, with or without "
      "preferential rights for the shareholders.")
NO_AUTH = ("Kommuniké från årsstämma. Stämman beslutade att fastställa resultat- och balansräkningen och "
           "bevilja ansvarsfrihet.")
BUYBACK = "Stämman beslutade om bemyndigande för styrelsen att förvärva egna aktier."


@pytest.fixture()
def settings(tmp_path):
    return replace(Settings.from_env(), db_path=tmp_path / "radar.db")


@pytest.fixture()
def connection(settings):
    connection = connect(settings.db_path)
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1,'Foo'),(2,'Bar')")
    return connection


def add_news(connection, document_id, company_id, title, published, text=None):
    with connection:
        connection.execute(
            "INSERT INTO news(document_id, company_id, title, published_at, url, text) VALUES (?,?,?,?,?,?)",
            (document_id, company_id, title, published, f"https://www.mfn.se/a/x/n{document_id}", text))


@pytest.mark.parametrize("title, expected", [
    ("Kommuniké från årsstämma i Foo AB", True),
    ("Kommuniké från extra bolagsstämma i Foo AB", True),
    ("Bulletin from the Annual General Meeting of Foo AB", True),
    ("Resolutions at the AGM of Foo", True),
    ("Kallelse till årsstämma i Foo AB", False),
    ("Notice of Annual General Meeting in Foo AB", False),
    ("Kallelse till extra bolagsstämma", False),
    ("Foo tecknar avtal", False),
])
def test_resolution_titles(title, expected):
    assert authorizations.is_resolution_title(title) is expected


def test_parse_swedish_percent_and_directed():
    found = authorizations.parse_authorization(SV, "2026-05-12T16:00:00+02:00")
    assert found["max_dilution_pct"] == 20.0 and found["max_shares"] is None
    assert found["directed_allowed"] == 1 and found["meeting_date"] == "2026-05-12"
    assert found["quote"].startswith("bemyndigande") and len(found["quote"]) <= 300


def test_parse_english_shares():
    found = authorizations.parse_authorization(EN, "2026-05-12")
    assert found["max_shares"] == 5_000_000 and found["directed_allowed"] == 1
    assert found["meeting_date"] == "2026-05-12"


def test_parse_shares_swedish_spaces_and_not_directed():
    text = ("Stämman bemyndigade styrelsen att besluta om nyemission av högst 5 000 000 aktier med företrädesrätt "
            "för aktieägarna.")
    found = authorizations.parse_authorization(text, "2026-05-12")
    assert found["max_shares"] == 5_000_000 and found["directed_allowed"] == 0


def test_parse_negative_cases():
    assert authorizations.parse_authorization(NO_AUTH, "2026-05-12") is None
    assert authorizations.parse_authorization(BUYBACK, "2026-05-12") is None
    assert authorizations.parse_authorization("", None) is None


def test_run_stores_found_and_skips_notices(settings, connection, monkeypatch):
    add_news(connection, 1, 1, "Kommuniké från årsstämma i Foo AB", "2026-05-12T16:00:00+02:00", text=SV)
    add_news(connection, 2, 2, "Bulletin from the Annual General Meeting of Bar", "2026-05-13T10:00:00+02:00")
    add_news(connection, 3, 1, "Kallelse till årsstämma i Foo AB", "2026-04-10T08:00:00+02:00", text=SV)
    add_news(connection, 4, 2, "Kommuniké från årsstämma i Bar", "2026-05-14T10:00:00+02:00", text=NO_AUTH)
    add_news(connection, 5, 1, "Kommuniké från årsstämma 2024", "2024-05-14T10:00:00+02:00", text=SV)  # för gammal
    fetched: list[str] = []

    def fake_fetch(url, *, timeout, user_agent):
        fetched.append(url)
        return EN
    monkeypatch.setattr(authorizations, "fetch_text", fake_fetch)
    counts = authorizations.run(connection, settings, today="2026-11-06", throttle=mfn.Throttle(0),
                                log=lambda _l: None)
    assert counts == {"candidates": 3, "fetched": 1, "found": 2, "failed": 0}
    assert fetched == ["https://www.mfn.se/a/x/n2"]
    assert {row["news_document_id"] for row in connection.execute("SELECT * FROM authorizations")} == {1, 2}
    latest = authorizations.latest(connection)
    assert latest[1]["max_dilution_pct"] == 20.0 and latest[2]["max_shares"] == 5_000_000
    again = authorizations.run(connection, settings, today="2026-11-06", throttle=mfn.Throttle(0),
                               log=lambda _l: None)
    assert again["found"] == 0 and again["candidates"] == 1   # bara kommunikén utan bemyndigande återstår


def test_run_survives_fetch_error_and_respects_limit(settings, connection, monkeypatch):
    add_news(connection, 1, 1, "Kommuniké från årsstämma i Foo AB", "2026-05-12")
    add_news(connection, 2, 2, "Kommuniké från årsstämma i Bar", "2026-05-13")

    def broken(url, *, timeout, user_agent):
        raise OSError("timeout")
    monkeypatch.setattr(authorizations, "fetch_text", broken)
    logged: list[str] = []
    counts = authorizations.run(connection, settings, limit=1, today="2026-11-06",
                                throttle=mfn.Throttle(0), log=logged.append)
    assert counts["candidates"] == 1 and counts["failed"] == 1 and counts["found"] == 0
    assert logged


def test_latest_prefers_newest_meeting(connection):
    with connection:
        for doc, day, pct in ((1, "2025-05-01", 10.0), (2, "2026-05-01", 20.0)):
            connection.execute(
                "INSERT INTO authorizations(company_id, news_document_id, meeting_date, max_dilution_pct, created_at)"
                " VALUES (1,?,?,?, 'x')", (doc, day, pct))
    assert authorizations.latest(connection)[1]["max_dilution_pct"] == 20.0
