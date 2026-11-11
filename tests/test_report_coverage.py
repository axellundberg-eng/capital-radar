"""Rapporttäckning: rubriker i nyhetsform, PDF-prov för bolag med gammal rapport och reservbilagor."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from radar import batch, collect, mfn
from radar.classify import classify
from radar.db import connect
from radar.settings import Settings

BASE = "https://www.mfn.se/a/balco-group/"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files",
                   legacy_db_path=tmp_path / "screener.db")


@pytest.mark.parametrize("title, attachments", [
    ("Rapport tredje kvartalet 2026 - Hedera Group", ()),
    ("Promimic AB: Tillväxt i kärnaffären i det andra kvartalet 2026", ()),
    ("Q2: Positiv EBITDA.", ()),
    ("Kvartrapport Q1", ()),
    ("En starkare verksamhet, redo för nästa kapitel", ("https://storage.mfn.se/x/bulten-q3-rapport-2026.pdf",)),
])
def test_report_titles_in_news_form_are_recognised(title, attachments):
    assert classify(title, None, attachments) == "report"


def test_invitations_and_plain_news_stay_as_before():
    assert classify("Inbjudan till presentation av rapport för tredje kvartalet", None, ()) == "calendar"
    assert classify("Bolaget tecknar avtal med ny kund", None, ()) == "other"


def _company(connection, published="2026-03-27"):
    with connection:
        connection.execute("INSERT INTO companies(id, name, mfn_slug) VALUES (1, 'Balco', 'balco-group')")
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at, pdf_url, status) "
                           "VALUES (5, 1, 'Årsredovisning 2025', ?, 'https://mb.cision.com/a.pdf', 'extracted')",
                           (published,))


def test_stale_company_pdf_is_probed_once_and_stored_as_report(settings, monkeypatch):
    connection = connect(settings.db_path)
    _company(connection)
    headline = mfn.MfnItem("Balco Group AB: Fortsatt fokus på högre marginaler", "2026-10-26T07:30:00+01:00",
                           BASE + "fortsatt-fokus", "sv", attachments=("https://mb.cision.com/Main/1/4338911.pdf",))
    other = mfn.MfnItem("Balco Group AB: Valberedning inför årsstämman", "2026-11-03T08:00:00+01:00",
                        BASE + "valberedning", "sv", attachments=("https://mb.cision.com/Main/1/999.pdf",))
    notice = mfn.MfnItem("Kallelse till extra bolagsstämma i Balco", "2026-11-04T08:00:00+01:00",
                         BASE + "kallelse", "sv", attachments=("https://mb.cision.com/Main/1/kallelse.pdf",))
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: [headline, other, notice])
    asked = []

    def probe(url):
        asked.append(url)
        return url.endswith("4338911.pdf")

    counts = collect.collect_company(connection, settings, {"id": 1, "name": "Balco", "mfn_slug": "balco-group"},
                                     probe=probe, today="2026-11-11")
    assert counts["probed_reports"] == 1 and counts["new_reports"] == 1 and counts["new_news"] == 2
    assert not any(url.endswith("kallelse.pdf") for url in asked)          # kallelser läses aldrig
    row = connection.execute("SELECT title, pdf_url, attachments FROM reports WHERE source_url=?",
                             (headline.url,)).fetchone()
    assert row[1].endswith("4338911.pdf") and json.loads(row[2]) == list(headline.attachments)
    # Bolaget har nu en färsk rapport: nästa varv läses inga PDF:er.
    asked.clear()
    collect.collect_company(connection, settings, {"id": 1, "name": "Balco", "mfn_slug": "balco-group"}, probe=probe,
                            today="2026-11-11")
    assert asked == []


def test_probe_result_is_cached_and_reads_report_words(settings):
    connection = connect(settings.db_path)

    def fetch(url, target, **_):
        target.write_bytes(b"%PDF")
        return target, "sha"

    calls = []

    def read(path):
        calls.append(path)
        return ["Delårsrapport januari – september 2026", "Balansräkning"]

    url = "https://mb.cision.com/Main/1/4338911.pdf"
    assert collect.probe_is_report(connection, settings, url, fetch=fetch, read=read)
    assert collect.probe_is_report(connection, settings, url, fetch=fetch, read=read)
    assert len(calls) == 1
    assert not collect.probe_is_report(connection, settings, "https://example.com/x.pdf", fetch=fetch, read=read)


def test_skipped_report_with_more_pdfs_gets_a_new_try_and_alternatives(settings):
    connection = connect(settings.db_path)
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'New Nordic')")
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at, pdf_url, source_url, "
                           "status) VALUES (7, 1, 'Interim Report Q3', '2026-10-30', 'https://cdn.bequoted.com/pr.pdf',"
                           " 'https://www.mfn.se/beq/a/nnh/q3', 'skipped')")
    item = mfn.MfnItem("Interim Report Q3", "2026-10-30T08:00:00+01:00", "https://www.mfn.se/beq/a/nnh/q3", "en",
                       attachments=("https://cdn.bequoted.com/pr.pdf", "https://cdn.bequoted.com/report-q3.pdf"))
    assert collect.store_attachments(connection, item)
    assert connection.execute("SELECT status FROM reports WHERE document_id=7").fetchone()[0] == "new"
    assert batch.other_pdfs(connection, 7) == ["https://cdn.bequoted.com/report-q3.pdf"]
    # Bilagorna sparas bara en gång: ingen ny återställning nästa varv.
    assert not collect.store_attachments(connection, item)
