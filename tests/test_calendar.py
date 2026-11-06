"""Tester för kalenderposter: datumtolkning, typ, lagring och nästa rapportdatum."""

from __future__ import annotations

from dataclasses import replace

import pytest

from radar import calendar, collect, mfn
from radar.db import connect
from radar.settings import Settings


@pytest.mark.parametrize("title, published, expected", [
    ("Delårsrapport januari-september publiceras den 22 oktober 2026", "2026-09-01T08:00:00+02:00", "2026-10-22"),
    ("Interim report Q3 will be published on 22 October 2026", "2026-09-01", "2026-10-22"),
    ("Q3 report on October 22, 2026", "2026-09-01", "2026-10-22"),
    ("Inbjudan till presentation 2026-10-22 kl 09:00", "2026-09-01", "2026-10-22"),
    ("Rapportpresentation onsdag den 11 november", "2026-10-20T08:00:00+02:00", "2026-11-11"),
    ("Delårsrapport publiceras 22 okt", "2026-09-01", "2026-10-22"),
    ("Bokslutskommuniké publiceras den 12 februari", "2026-11-20", "2027-02-12"),   # före publiceringen: nästa år
    ("Inbjudan till presentation av delårsrapporten", "2026-10-20", None),
    ("Finansiell kalender", "2026-10-20", None),
    ("Rapport den 31 februari 2026", "2026-01-01", None),                           # ogiltigt datum
])
def test_parse_event_date(title, published, expected):
    assert calendar.parse_event_date(title, published) == expected


def test_parse_event_date_without_roll_forward():
    assert calendar.parse_event_date("Stämman hölls den 12 maj", "2026-06-01", roll_forward=False) == "2026-05-12"


@pytest.mark.parametrize("title, kind", [
    ("Delårsrapport januari-september 2026", "report"),
    ("Bokslutskommuniké 2026", "report"),
    ("Interim report Q2 2026", "report"),
    ("Year-end report 2026 presentation", "report"),
    ("Inbjudan till rapportpresentation", "report"),
    ("Kallelse till årsstämma i Foo AB", "meeting"),
    ("Notice of Annual General Meeting", "meeting"),
    ("Notice of EGM", "meeting"),
    ("Finansiell kalender 2027", "other"),
])
def test_kind_of(title, kind):
    assert calendar.kind_of(title) == kind


def item(title, published, url):
    return mfn.MfnItem(title, published, url, "sv")


def test_store_item_and_next_report(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1,'A'),(2,'B')")
    assert calendar.store_item(connection, 1, item("Delårsrapport publiceras den 22 oktober 2026",
                                                   "2026-09-01T08:00:00+02:00", "https://x/a1"))
    assert not calendar.store_item(connection, 1, item("Dubblett", "2026-09-01", "https://x/a1"))
    calendar.store_item(connection, 1, item("Bokslutskommuniké den 12 februari 2027", "2026-09-02", "https://x/a2"))
    calendar.store_item(connection, 1, item("Kallelse till årsstämma den 5 maj 2027", "2026-09-02", "https://x/a3"))
    calendar.store_item(connection, 2, item("Interim report Q3 on 2026-10-01", "2026-09-01", "https://x/b1"))
    calendar.store_item(connection, 2, item("Inbjudan till presentation av delårsrapporten", "2026-09-01",
                                            "https://x/b2"))
    upcoming = calendar.next_report(connection, "2026-10-15")
    assert upcoming == {1: {"date": "2026-10-22", "title": "Delårsrapport publiceras den 22 oktober 2026"}}
    assert calendar.next_report(connection, "2026-10-23")[1]["date"] == "2027-02-12"
    assert calendar.next_report(connection, "2026-09-15")[2]["date"] == "2026-10-01"


def test_collect_company_stores_calendar_items(tmp_path, monkeypatch):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db")
    connection = connect(settings.db_path)
    with connection:
        connection.execute("INSERT INTO companies(id, name, mfn_slug) VALUES (1,'A','a')")
    invite = mfn.MfnItem("Inbjudan till presentation av delårsrapport den 22 oktober 2026",
                         "2026-10-01T08:00:00+02:00", "https://www.mfn.se/a/a/inbjudan", "sv")
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: [invite])
    company = {"id": 1, "name": "A", "mfn_slug": "a"}
    counts = collect.collect_company(connection, settings, company)
    assert counts["calendar"] == 1 and counts["new_news"] == 0
    assert collect.collect_company(connection, settings, company)["calendar"] == 0
    assert calendar.next_report(connection, "2026-10-02")[1]["date"] == "2026-10-22"
