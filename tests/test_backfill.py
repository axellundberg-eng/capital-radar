"""Bakåtgranskning av finansieringsnyheter och tvillingfiltret (svensk och engelsk version samma dag)."""

from __future__ import annotations

import json
from dataclasses import replace

from radar import news
from radar.db import connect
from radar.settings import Settings

TODAY = "2026-11-11"
ANSWER = json.dumps({"is_financing": True, "unit": "MSEK", "currency": "SEK", "events": [
    {"type": "rights_issue", "status": "completed", "gross_amount": 21, "date": "2026-09-07", "quote": "utfall"}]})


class FakeClient:
    def __init__(self):
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        return ANSWER, {"prompt_tokens": 5000, "completion_tokens": 100}


def _row(company_id, title, day):
    return {"company_id": company_id, "title": title, "published_at": day}


def test_english_twin_is_skipped_only_when_a_swedish_version_exists_the_same_day():
    rows = [_row(1, "Image Systems AB offentliggör utfall i företrädesemissionen", "2026-09-07"),
            _row(1, "Image Systems AB announces outcome of the rights issue", "2026-09-07"),
            _row(2, "Bolaget announces outcome of the directed issue", "2026-09-07")]   # bara engelska
    keep, twins = news.split_twins(rows)
    assert [r["title"] for r in twins] == ["Image Systems AB announces outcome of the rights issue"]
    assert len(keep) == 2


def _seed(connection):
    result = lambda runway: json.dumps({"status": "ok", "runway_today": runway, "period_end": "2026-09-30",
                                        "flags": [], "cash_flow_positive": False})
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'Mål AB'), (2, 'Lång AB')")
        for company, doc, runway in ((1, 10, 5.0), (2, 20, 30.0)):
            connection.execute("INSERT INTO reports(document_id, company_id, title, published_at) VALUES (?,?,'Q3','2026-10-20')",
                               (doc, company))
            connection.execute("INSERT INTO runway_results(company_id, document_id, computed_at, result_json) VALUES (?,?,?,?)",
                               (company, doc, TODAY, result(runway)))
        connection.executemany(
            "INSERT INTO news(document_id, company_id, title, published_at, url, text) VALUES (?,?,?,?,?,?)", [
                # Före rapportperioden: läses bara av bakåtgranskningen.
                (100, 1, "Mål AB offentliggör utfall i företrädesemissionen", "2026-09-07", "u", "Utfall: 21 MSEK."),
                (101, 1, "Mål AB announces outcome of the rights issue", "2026-09-07", "u", "Outcome: SEK 21m."),
                (102, 1, "Mål AB tecknar avtal med kund", "2026-09-08", "u", "Avtal."),        # inte finansiering
                (103, 1, "Mål AB genomför riktad emission", "2023-01-10", "u", "För gammal."),   # äldre än 24 mån
                (200, 2, "Lång AB genomför riktad nyemission", "2026-08-01", "u", "Riktad emission 10 MSEK."),
            ])


def test_backfill_reads_pre_period_news_target_group_first_and_stops_at_budget(tmp_path):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connection = connect(settings.db_path)
    _seed(connection)
    assert [(c, p) for c, _d, p in news.backfill_targets(connection)] == [(1, 0), (2, 1)]
    client = FakeClient()
    counts = news.run_backfill(settings, client, today=TODAY, token_budget=5_000, workers=1, log=lambda _l: None)
    # Budgeten räcker till en nyhet: målgruppens svenska version, inte tvillingen och inte bolag 2.
    assert len(client.calls) == 1 and "offentliggör utfall" in client.calls[0]
    assert counts["financing"] == 1 and counts["twins"] == 1 and counts["budget_slut"] == 1
    status = dict(connection.execute("SELECT document_id, status FROM news").fetchall())
    assert status == {100: "financing", 101: "twin", 102: "new", 103: "new", 200: "new"}
    event = connection.execute("SELECT type, status, gross_amount FROM financing_events WHERE company_id=1").fetchone()
    assert tuple(event) == ("rights_issue", "completed", 21e6)
    # Nästa varv fortsätter med prioritet 1; redan granskade och tvillingar läses aldrig igen.
    news.run_backfill(settings, client, today=TODAY, token_budget=50_000, workers=1, log=lambda _l: None)
    assert len(client.calls) == 2 and "Lång AB" in client.calls[1]


def test_out_of_credit_stops_the_run_and_leaves_news_unreviewed(tmp_path):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connection = connect(settings.db_path)
    _seed(connection)

    class Broke:
        calls = 0

        def complete(self, system, user):
            Broke.calls += 1
            raise RuntimeError("Error code: 402 - {'error': {'message': 'Insufficient Balance'}}")

    counts = news.run_backfill(settings, Broke(), today=TODAY, token_budget=10**9, workers=1, log=lambda _l: None)
    assert Broke.calls == 1 and counts["out_of_credit"] == 1 and counts["budget_slut"] == 1
    status = dict(connection.execute("SELECT document_id, status FROM news").fetchall())
    assert status[100] == "new" and status[200] == "new"        # inget markerat som misslyckat


def test_informational_news_get_no_ai_call(tmp_path):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connection = connect(settings.db_path)
    _seed(connection)
    with connection:
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url, text) VALUES "
                           "(104, 1, 'Sista dag för handel med uniträtter i Mål AB:s företrädesemission', '2026-08-20', 'u', 'x')")
    client = FakeClient()
    counts = news.run_backfill(settings, client, today=TODAY, token_budget=10**9, workers=1, log=lambda _l: None)
    assert counts["info"] == 1 and all("Sista dag" not in call for call in client.calls)
    assert connection.execute("SELECT status FROM news WHERE document_id=104").fetchone()[0] == "info"
