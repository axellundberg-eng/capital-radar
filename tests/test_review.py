"""Granskning: rimlighetskontroller, rättelser som överlever omextraktion, och CLI-kommandona."""

from __future__ import annotations

import json

import pytest

from radar import review
from radar.db import connect
from radar.financing import load_events
from radar.pipeline import refresh_runway
from radar.qualify import load_history, recent_financing

END = "2026-06-30"
TODAY = "2026-09-30"


def seed(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'Testbolaget')")
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at, status) "
                           "VALUES (10, 1, 'Delårsrapport', '2026-08-15', 'extracted')")
        cursor = connection.execute(
            "INSERT INTO extractions(document_id, model, pages, status, response_json, created_at) "
            "VALUES (10, 'test', '[]', 'ok', ?, '2026-08-15')", (json.dumps({"report_period_end": END}),))
        extraction = cursor.lastrowid
        connection.executemany(
            "INSERT INTO facts(extraction_id, document_id, company_id, kind, value, currency, period_start, "
            "period_end, page, quote) VALUES (?, 10, 1, ?, ?, 'SEK', ?, ?, 5, ?)",
            [(extraction, "cash", 12e6, None, END, "Likvida medel 12 000 20 000"),
             (extraction, "operating_cash_flow", 6e6, "2026-01-01", END, "Kassaflöde 6 000"),
             (extraction, "operating_cash_flow", -6e6, "2026-04-01", END, "Kassaflöde -6 000")])
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url, status) "
                           "VALUES (20, 1, 'Riktad emission', '2026-08-01', 'https://mfn.se/a/1', 'financing')")
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url, status) "
                           "VALUES (21, 1, 'Directed issue', '2026-08-01', 'https://mfn.se/a/2', 'financing')")
        connection.executemany(
            "INSERT INTO financing_events(news_document_id, company_id, type, status, gross_amount, currency, "
            "event_date, quote, created_at) VALUES (?, 1, 'directed_issue', 'completed', 10000000, 'SEK', "
            "'2026-08-01', 'citat', '2026-08-01')", [(20,), (21,)])
    return connection


def test_note_number_is_not_a_column():
    # "11" är ett notnummer, inte en kolumn; värdet matchar första beloppet.
    assert review.column_mismatch(20_630_000, "Kassa och bank 11 20 630 35 765") is None
    assert review.column_mismatch(62_198_000, "Cash and cash equivalents at end of period 26 62,198 64,310") is None
    # Värdet matchar jämförelsekolumnen: troligen fel period.
    assert "kolumn 2" in review.column_mismatch(35_765_000, "Kassa och bank 20 630 35 765 50 323")


def test_positive_but_burning_is_flagged(tmp_path):
    connection = seed(tmp_path)
    refresh_runway(connection, 10, today=TODAY)
    checks = {f.check for f in review.run_checks(connection, TODAY)}
    assert "positive_but_burning" in checks      # halvåret positivt, senaste kvartalet bränner


def test_fact_correction_changes_runway_survives_reextraction_and_can_be_reverted(tmp_path):
    connection = seed(tmp_path)
    before = refresh_runway(connection, 10, today=TODAY)
    assert before["cash_flow_positive"]          # den längsta perioden (halvåret) är positiv
    correction = review.add_correction(
        connection, target="fact", field="delete", reason="Q1 hade en engångsinbetalning", company_id=1,
        document_id=10, kind="operating_cash_flow", period_start="2026-01-01", period_end=END)
    after = refresh_runway(connection, 10, today=TODAY)
    assert not after["cash_flow_positive"] and after["monthly_burn"] == pytest.approx(2e6)
    # En ny extraktion med samma fakta skriver inte över rättelsen.
    with connection:
        cursor = connection.execute(
            "INSERT INTO extractions(document_id, model, pages, status, response_json, created_at) "
            "VALUES (10, 'test', '[]', 'ok', ?, '2026-09-01')", (json.dumps({"report_period_end": END}),))
        connection.execute(
            "INSERT INTO facts(extraction_id, document_id, company_id, kind, value, currency, period_start, period_end) "
            "SELECT ?, document_id, company_id, kind, value, currency, period_start, period_end FROM facts "
            "WHERE extraction_id=1", (cursor.lastrowid,))
    assert not refresh_runway(connection, 10, today=TODAY)["cash_flow_positive"]
    review.revert_correction(connection, correction)
    assert refresh_runway(connection, 10, today=TODAY)["cash_flow_positive"]


def test_value_correction_and_period_correction(tmp_path):
    connection = seed(tmp_path)
    review.add_correction(connection, target="fact", field="value", reason="TSEK lästes som SEK", company_id=1,
                          document_id=10, kind="cash", period_end=END, new_value=24e6)
    assert refresh_runway(connection, 10, today=TODAY)["cash"] == 24e6
    with pytest.raises(ValueError):
        review.add_correction(connection, target="fact", field="value", reason="  ", company_id=1,
                              document_id=10, kind="cash", period_end=END, new_value=1)
    with pytest.raises(ValueError):
        review.add_correction(connection, target="fact", field="type", reason="x", company_id=1, document_id=10)


def test_hidden_event_disappears_and_listing_merges_languages(tmp_path):
    connection = seed(tmp_path)
    assert len(load_events(connection, 1)) == 2
    count, listing = recent_financing(load_history(connection)[1], TODAY)
    assert count == 1 and len(listing) == 1      # svensk och engelsk version visas en gång
    event_id = connection.execute("SELECT MIN(id) FROM financing_events").fetchone()[0]
    review.add_correction(connection, target="event", field="hide", reason="dubblett", company_id=1,
                          event_id=event_id, new_value="1")
    assert len(load_events(connection, 1)) == 1
    review.add_correction(connection, target="event", field="status", reason="bara annonserad", company_id=1,
                          event_id=event_id + 1, new_value="announced")
    assert [e.status for e in load_events(connection, 1)] == ["announced"]


def test_queue_and_cli(tmp_path, monkeypatch, capsys):
    from radar.__main__ import main

    connection = seed(tmp_path)
    refresh_runway(connection, 10, today=TODAY)
    items = review.queue(connection, since="2026-01-01")
    assert items[0]["name"] == "Testbolaget" and items[0]["priority"] == 3
    assert items[0]["facts"] and items[0]["events"]
    connection.close()
    monkeypatch.setenv("RADAR_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    assert main(["correct", "fact", "--document", "10", "--kind", "operating_cash_flow", "--period-start",
                 "2026-01-01", "--period-end", END, "--delete", "--reason", "engångsinbetalning"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["runway"]["cash_flow_positive"] is False
    assert main(["correct", "event", "--id", "1", "--reason", "x"]) == 2          # inget att rätta
    assert main(["corrections", "revert", str(out["corrections"][0])]) == 0
    assert main(["review", "log", "--checked", "1", "--corrections", "1", "--before", "3"]) == 0


def test_truncated_answer_is_not_retried_every_cycle(tmp_path):
    from radar.pipeline import _fail

    connection = seed(tmp_path)
    assert _fail(connection, 10, "extrahera", "tomt svar (finish_reason=length)")["status"] == "too_long"
    assert connection.execute("SELECT status FROM reports WHERE document_id=10").fetchone()[0] == "too_long"
    assert _fail(connection, 10, "hämta", "timeout")["status"] == "failed"


def test_year_is_not_split_into_fragments():
    # "2026" ska inte bli "202" + "6" och se ut som en första kolumn.
    assert review.column_mismatch(67_000, "Likvida medel uppgick per 30 september 2026 till 67 KSEK (1 574)") is None
    assert review._numbers("Kassa Q3 2026 R12 8 612 19 884") == [3.0, 2026.0, 12.0, 8612.0, 19884.0]


def test_scale_correction_multiplies_the_whole_report(tmp_path):
    connection = seed(tmp_path)
    before = refresh_runway(connection, 10, today=TODAY)
    review.add_correction(connection, target="fact", field="scale", reason="TSEK lästes som SEK", company_id=1,
                          document_id=10, new_value=1000)
    after = refresh_runway(connection, 10, today=TODAY)
    assert after["cash"] == before["cash"] * 1000
    assert after["periods"][0]["operating_cash_flow"] == before["periods"][0]["operating_cash_flow"] * 1000
