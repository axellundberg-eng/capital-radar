"""Ägartolkning: JSON-tolkning, sidurval, hoppa-över-logik och tokenbudget (falsk klient, inget nätverk)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from radar import owners
from radar.db import connect
from radar.settings import Settings

TABLE_PAGE = "Största aktieägare per 2026-09-30\nAktieägare Antal aktier Kapital Röster\nAlfa Invest 1 000 000 40,0 % 45,0 %"
OTHER_PAGE = "Koncernens resultaträkning"

REPLY = json.dumps({"as_of": "2026-09-30", "rows": [
    {"name": "Alfa Invest", "shares": "1 000 000", "pct_capital": "40,0", "pct_votes": 45},
    {"name": "Beta AB", "shares": None, "pct_capital": 12.5, "pct_votes": None},
    {"name": "Gamma", "shares": 5, "pct_capital": 150, "pct_votes": None},   # omöjlig procent blir null
    {"name": "", "shares": 1}]})


class FakeClient:
    def __init__(self, reply: str = REPLY, tokens: int = 100) -> None:
        self.reply, self.tokens, self.calls = reply, tokens, []

    def complete(self, system, user):
        self.calls.append(user)
        return self.reply, {"prompt_tokens": self.tokens, "completion_tokens": 10}


def quiet(_line):
    return None


@pytest.fixture()
def connection(tmp_path):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db")
    conn = connect(settings.db_path)
    with conn:
        conn.execute("INSERT INTO companies(id, name) VALUES (1,'Alfa'),(2,'Beta'),(3,'Gamma')")
        for doc, company, published in ((10, 1, "2026-10-01"), (11, 1, "2026-07-01"), (20, 2, "2026-10-01"),
                                        (30, 3, "2026-10-01")):
            conn.execute("INSERT INTO reports(document_id, company_id, title, published_at) VALUES (?,?,?,?)",
                         (doc, company, f"Rapport {doc}", published))
        conn.executemany("INSERT INTO report_pages(document_id, page, text) VALUES (?,?,?)", [
            (10, 1, OTHER_PAGE), (10, 2, TABLE_PAGE), (11, 1, TABLE_PAGE),
            (20, 1, OTHER_PAGE), (30, 1, TABLE_PAGE), (30, 2, TABLE_PAGE), (30, 3, TABLE_PAGE)])
    return conn


def test_parse_owners_is_strict_about_values():
    as_of, rows = owners.parse_owners("```json\n" + REPLY + "\n```")
    assert as_of == "2026-09-30" and [r["name"] for r in rows] == ["Alfa Invest", "Beta AB", "Gamma"]
    assert rows[0] == {"name": "Alfa Invest", "shares": 1_000_000.0, "pct_capital": 40.0, "pct_votes": 45.0}
    assert rows[1]["shares"] is None and rows[1]["pct_votes"] is None
    assert rows[2]["pct_capital"] is None
    with pytest.raises(Exception):
        owners.parse_owners("inte json")


def test_parse_owners_limits_to_fifteen_rows():
    raw = json.dumps({"as_of": None, "rows": [{"name": f"Ägare {i}", "pct_capital": 1} for i in range(30)]})
    assert len(owners.parse_owners(raw)[1]) == 15


def test_run_stores_owners_sends_only_matching_pages_and_skips_processed(connection):
    client = FakeClient()
    counts = owners.run(connection, None, client, log=quiet, today="2026-11-07")
    assert counts["ok"] == 2 and counts["no_table"] == 1 and counts["failed"] == 0
    assert len(client.calls) == 2                                     # bolag 2 saknar tabell: inget anrop
    first = client.calls[0]
    assert "Största aktieägare" in first and OTHER_PAGE not in first   # bara matchande sida
    assert client.calls[1].count("--- Sida") == 2                      # högst två sidor (bolag 3 har tre)
    stored = owners.latest(connection, 1)
    assert stored["as_of"] == "2026-09-30" and stored["source_document_id"] == 10
    assert stored["top"][0]["name"] == "Alfa Invest" and stored["top3_pct"] == 52.5
    assert stored["free_float_estimate_pct"] == 47.5                   # 100 - (40 + 12.5)
    row = connection.execute("SELECT status, tokens FROM owner_fetch WHERE company_id=2").fetchone()
    assert (row["status"], row["tokens"]) == ("no_table", 0)
    again = FakeClient()
    second = owners.run(connection, None, again, log=quiet, today="2026-11-07")
    assert again.calls == [] and second["skipped"] == 3 and second["processed"] == 0


def test_new_matching_document_is_processed_again(connection):
    owners.run(connection, None, FakeClient(), log=quiet, today="2026-11-07")
    with connection:
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at) VALUES (12,1,'Ny','2026-11-05')")
        connection.execute("INSERT INTO report_pages(document_id, page, text) VALUES (12,1,?)", (TABLE_PAGE,))
    client = FakeClient()
    counts = owners.run(connection, None, client, log=quiet, today="2026-11-07")
    assert len(client.calls) == 1 and counts["ok"] == 1
    assert owners.latest(connection, 1)["source_document_id"] == 12


def test_token_budget_and_limit_stop_processing(connection):
    client = FakeClient(tokens=500)
    counts = owners.run(connection, None, client, token_budget=300, log=quiet, today="2026-11-07")
    assert len(client.calls) == 1 and counts["remaining"] == 1 and counts["tokens"] == 510
    client2 = FakeClient()
    owners.run(connection, None, client2, limit=1, log=quiet, today="2026-11-08")
    assert len(client2.calls) == 1


def test_broken_reply_is_recorded_as_failed_and_retried_next_day(connection):
    counts = owners.run(connection, None, FakeClient("inte json", tokens=7), log=quiet, today="2026-11-07")
    assert counts["failed"] == 2 and counts["tokens"] == 2 * 2 * 17      # två försök per bolag
    again = FakeClient()
    assert owners.run(connection, None, again, log=quiet, today="2026-11-07")["processed"] == 0
    assert owners.run(connection, None, again, log=quiet, today="2026-11-08")["ok"] == 2
