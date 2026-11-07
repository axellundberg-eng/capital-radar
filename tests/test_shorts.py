"""Blankningsregistret: ODS-tolkning, bolagsmatchning och ögonblicksbild (fixtur, inget nätverk)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from xml.sax.saxutils import escape

import pytest

from radar import shorts
from radar.db import connect
from radar.settings import Settings

HEADER = ["Innehavare av positionen (Position holder)", "Namn på emittent (Name of the issuer)", "ISIN",
          "Position i procent(Position in per cent)", "Datum för positionen (Position date)",
          "Kommentar(Comment)"]
ROWS = [
    ["Two Sigma Investments, LP", "Alfa Bolag AB (publ)", "SE0000000001", "1,5", "2026-11-06"],
    ["Citadel Securities (Europe) Limited", "Alfa Bolag AB", "SE0000000001", "0,8", "2026-11-05"],
    ["Argonaut Capital Partners LLP", "Beta Group AB", "SE9999999999", "0,71", "2026-11-04"],   # fel ISIN, namn matchar
    ["Okänd Fond", "Främmande Bolag AB", "SE0000000777", "2,0", "2026-11-03"],                 # inget bolag
    ["Trasig", "Alfa Bolag AB", "SE0000000001", "ingen", "2026-11-03"],                         # ogiltig procent
]


def make_ods(rows=ROWS, header=HEADER) -> bytes:
    def cell(text):
        return f'<table:table-cell office:value-type="string"><text:p>{escape(text)}</text:p></table:table-cell>'

    tail = '<table:table-cell table:number-columns-repeated="16000"/>'
    body = ('<table:table-row><table:table-cell office:value-type="string"><text:p>Aktuella positioner</text:p>'
            f'</table:table-cell>{tail}</table:table-row>')
    body += '<table:table-row table:number-rows-repeated="3"><table:table-cell table:number-columns-repeated="16384"/></table:table-row>'
    for row in ([header] if header else []) + rows:
        body += "<table:table-row>" + "".join(cell(c) for c in row) + tail + "</table:table-row>"
    xml = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
           'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
           'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
           f'<table:table table:name="Blad1">{body}</table:table></office:spreadsheet></office:body></office:document-content>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", xml)
    return buffer.getvalue()


def quiet(_line):
    return None


@pytest.fixture()
def connection(tmp_path):
    conn = connect(replace(Settings.from_env(), db_path=tmp_path / "radar.db").db_path)
    with conn:
        conn.execute("INSERT INTO companies(id, name, isin) VALUES (1,'Alfa Bolag AB','SE0000000001'),"
                     "(2,'Beta Group AB (publ)','SE0000000002'),(3,'Gamma','SE0000000003')")
    return conn


def test_read_and_parse_ods():
    positions = shorts.parse_positions(shorts.read_ods(make_ods()))
    assert len(positions) == 4                                       # raden med ogiltig procent hoppas över
    assert positions[0] == {"holder": "Two Sigma Investments, LP", "issuer": "Alfa Bolag AB (publ)",
                            "isin": "SE0000000001", "pct": 1.5, "date": "2026-11-06"}


def test_bad_files_raise():
    with pytest.raises(shorts.ShortError):
        shorts.read_ods(b"<html>inte en ods</html>")
    with pytest.raises(shorts.ShortError):
        shorts.parse_positions([["bara", "text"]])


def test_matching_prefers_isin_then_normalized_name(connection):
    matched = shorts.match_positions(connection, shorts.parse_positions(shorts.read_ods(make_ods())))
    assert [(m["company_id"], m["pct"]) for m in matched] == [(1, 1.5), (1, 0.8), (2, 0.71)]


def test_run_replaces_snapshot_once_per_day_and_keeps_it_on_failure(connection):
    counts = shorts.run(connection, None, log=quiet, fetch=lambda _u: make_ods(), today="2026-11-07")
    assert counts["fetched"] == 1 and counts["rows"] == 4 and counts["matched"] == 3
    assert shorts.for_company(connection, 1) == [
        {"holder": "Two Sigma Investments, LP", "pct": 1.5, "date": "2026-11-06"},
        {"holder": "Citadel Securities (Europe) Limited", "pct": 0.8, "date": "2026-11-05"}]

    def never(_url):
        raise AssertionError("hämtades igen")

    assert shorts.run(connection, None, log=quiet, fetch=never, today="2026-11-07")["skipped"] == 1
    # nästa dag: ny ögonblicksbild ersätter den gamla
    smaller = make_ods([["Ny Fond", "Gamma", "SE0000000003", "3,1", "2026-11-08"]])
    shorts.run(connection, None, log=quiet, fetch=lambda _u: smaller, today="2026-11-08")
    assert shorts.for_company(connection, 1) == [] and shorts.for_company(connection, 3)[0]["pct"] == 3.1

    def broken(_url):
        raise shorts.ShortError("nere")

    failed = shorts.run(connection, None, log=quiet, fetch=broken, today="2026-11-09")
    assert failed["failed"] == 1 and shorts.for_company(connection, 3)         # gamla bilden kvar
    assert shorts.run(connection, None, log=quiet, fetch=lambda _u: smaller, today="2026-11-09")["fetched"] == 1
