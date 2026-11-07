"""Insynshandel ur FI:s register: kodning, CSV, bolagsmatchning, lagring och sammanfattning (inget nätverk)."""

from __future__ import annotations

import pytest

from radar import insiders
from radar.db import connect

HEADER = ("Publiceringsdatum;Emittent;LEI-kod;Anmälningsskyldig;Person i ledande ställning;Befattning;Närstående;"
          "Korrigering;Beskrivning av korrigering;Är förstagångsrapportering;Är kopplad till aktieprogram;Karaktär;"
          "Instrumenttyp;Instrumentnamn;ISIN;Transaktionsdatum;Volym;Volymsenhet;Pris;Valuta;Handelsplats;Status;")


def line(published, issuer, person, kind, volume, price, day, isin="SE0001", status="Aktuell", unit="Antal",
         currency="SEK"):
    return (f"{published};{issuer};LEI;{person};{person};Styrelseledamot;;;;Ja;;{kind};Aktie;{issuer};{isin};"
            f"{day} 00:00:00;{volume};{unit};{price};{currency};XSTO;{status};")


CSV = "\r\n".join([
    HEADER,
    line("2026-10-20 09:00:00", "Foo AB (publ)", "Åsa Öberg", "Förvärv", "10000,0", "12,5", "2026-10-19"),
    line("2026-10-21 09:00:00", "Foo AB (publ)", "Åsa Öberg", "Avyttring", "1 000", "20,00", "2026-10-20"),
    line("2026-10-22 09:00:00", "Foo AB (publ)", "Per Persson", "Förvärv", "500,0", "10,0", "2026-10-21",
         status="Struken"),
    line("2026-10-23 09:00:00", "Foo Bar Invest AB", "Lars Larsson", "Förvärv", "100,0", "10,0", "2026-10-22",
         isin="SE9999"),
    line("2026-09-01 09:00:00", "Foo AB (publ)", "Åsa Öberg", "Teckning", "300,0", "9,0", "2026-08-30",
         isin="SE0001W"),
]) + "\r\n"


def test_decode_handles_utf16_without_bom_utf16_bom_utf8_and_latin1():
    assert insiders.decode_export(CSV.encode("utf-16-le")) == CSV
    assert insiders.decode_export(CSV.encode("utf-16")) == CSV
    assert insiders.decode_export(CSV.encode("utf-8")) == CSV
    assert insiders.decode_export(CSV.encode("utf-8-sig")).lstrip("﻿") == CSV
    assert insiders.decode_export(CSV.encode("latin-1")) == CSV


def test_parse_csv_reads_swedish_headers_and_rejects_html():
    rows = insiders.parse_csv(insiders.decode_export(CSV.encode("utf-16-le")))
    assert len(rows) == 5 and rows[0]["Person i ledande ställning"] == "Åsa Öberg" and rows[0]["Karaktär"] == "Förvärv"
    with pytest.raises(insiders.InsiderError):
        insiders.parse_csv("<!DOCTYPE html><html>")


def test_numbers_use_swedish_formats():
    assert insiders.parse_number("911950,0") == 911950.0
    assert insiders.parse_number("16 244") == 16244.0 and insiders.parse_number("22,64") == 22.64
    assert insiders.parse_number("") is None


def test_name_normalization_is_conservative():
    assert insiders.normalize_name("Foo AB (publ)") == insiders.normalize_name("FOO Aktiebolag") == "foo"
    assert insiders.normalize_name("Foo-Bar, A.B.") != insiders.normalize_name("Foo")
    assert insiders.normalize_name("Foo Bar Invest AB") != insiders.normalize_name("Foo AB")
    assert insiders.search_term("Foo AB (publ)") == "Foo"


@pytest.fixture()
def connection(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin) VALUES (1,'Foo AB','SE0001')")
        connection.execute("INSERT INTO reports(document_id, company_id, title) VALUES (1,1,'Q3')")
        connection.execute("INSERT INTO runway_results VALUES (1,1,'x','{}')")
    return connection


def test_ingest_matches_by_name_skips_cancelled_and_dedupes(connection):
    rows = insiders.parse_csv(CSV)
    result = insiders.ingest(connection, 1, "Foo AB", "SE0001", rows)
    # "Foo Bar Invest AB" (annan utgivare, annat ISIN) matchar inte; den strukna raden matchar men sparas inte
    assert result["rows"] == 5 and result["matched"] == 4 and result["unmatched"] == 1 and result["new"] == 3
    assert insiders.ingest(connection, 1, "Foo AB", "SE0001", rows)["new"] == 0
    assert connection.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0] == 3


def test_run_fetches_once_per_day_and_records_failures(connection):
    urls: list[str] = []

    def fetch(url):
        urls.append(url)
        return CSV.encode("utf-16-le")

    counts = insiders.run(connection, None, fetch=fetch, sleep=lambda _: None, today="2026-11-07", log=lambda _: None)
    assert counts["fetched"] == 1 and counts["new_trades"] == 3 and counts["unmatched_rows"] == 1
    assert "Utgivare=Foo&" in urls[0] and "Transaktionsdatum.From=2025-11-07" in urls[0] and "button=export" in urls[0]
    insiders.run(connection, None, fetch=fetch, sleep=lambda _: None, today="2026-11-07", log=lambda _: None)
    assert len(urls) == 1
    bad = insiders.run(connection, None, fetch=lambda url: b"<html>", sleep=lambda _: None, today="2026-11-08",
                       log=lambda _: None)
    assert bad["failed"] == 1
    assert connection.execute("SELECT error FROM insider_fetch").fetchone()[0].startswith("svaret är ingen CSV")


def test_summary_and_recent_list(connection):
    insiders.ingest(connection, 1, "Foo AB", "SE0001", insiders.parse_csv(CSV))
    summary = insiders.summary(connection, 1, "2026-11-07")
    assert summary == {"months": 6, "buys": 125000, "sells": 20000, "net": 105000, "buyers": 1, "sellers": 1}
    listing = insiders.recent(connection, 1)
    assert [t["transaction"] for t in listing] == ["Avyttring", "Förvärv", "Teckning"]   # nyast först
    assert listing[1]["value"] == 125000.0 and listing[0]["person"] == "Åsa Öberg"
    assert insiders.summary(connection, 1, "2027-06-01")["buys"] == 0                  # utanför fönstret
