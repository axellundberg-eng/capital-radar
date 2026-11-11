"""Markörer (kallelse, going concern, lån, brygglån, TO-period m.fl.) och branschklassning."""

from __future__ import annotations

import json
import sqlite3

import pytest

from radar import markers, plans, sectors
from radar.db import connect

TODAY = "2026-11-11"


# --- Kallelse till extra stämma och going concern -----------------------------------------------------------

def test_egm_notice_with_issue_on_agenda_is_found_but_not_incentives_or_annual_meetings():
    title = "Kallelse till extra bolagsstämma i Bolaget AB"
    agenda = ("Dagordning. 1. Val av ordförande. 7. Beslut om godkännande av styrelsens beslut om företrädesemission "
              "av units. 8. Stämmans avslutande.")
    found = plans.find_egm_issue(title, agenda)
    assert found["kind"] == "egm_called" and "företrädesemission" in found["quote"]
    english = plans.find_egm_issue("Notice of Extraordinary General Meeting in Bolaget AB",
                                   "Proposal for resolution on authorisation for the board to resolve on new issue of shares.")
    assert english is not None
    incentive = "7. Beslut om riktad emission av teckningsoptioner inom ramen för incitamentsprogram till anställda."
    assert plans.find_egm_issue(title, incentive) is None
    assert plans.find_egm_issue("Kallelse till årsstämma i Bolaget AB", agenda) is None    # bara extra stämma


def test_going_concern_is_found_in_report_text():
    text = ("Styrelsen bedömer att det finns en väsentlig osäkerhetsfaktor som kan leda till betydande tvivel om "
            "bolagets förmåga att fortsätta verksamheten enligt antagandet om fortsatt drift.")
    assert plans.find_going_concern(text)["kind"] == "going_concern"
    assert plans.find_going_concern("Bolaget har en stark finansiell ställning.") is None


def test_run_reads_notices_only_as_notices_and_fetches_them_once(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'A')")
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url) VALUES "
                           "(5, 1, 'Kallelse till extra bolagsstämma i A', '2026-11-01', 'https://www.mfn.se/a/a/k')")

    calls = []

    def fetch(url):
        calls.append(url)
        return "7. Beslut om nyemission av aktier med företrädesrätt. Styrelsen avser att genomföra en emission."

    class NoWait:
        def wait(self):
            pass

    counts = plans.fetch_notices(connection, None, today=TODAY, fetch=fetch, throttle=NoWait())
    assert counts == {"candidates": 1, "fetched": 1, "failed": 0}
    assert plans.fetch_notices(connection, None, today=TODAY, fetch=fetch, throttle=NoWait())["candidates"] == 0
    result = plans.run(connection, today=TODAY)
    assert result["egm_called"] == 1 and result["plan"] == 0      # förslagstext i kallelsen är ingen plan
    assert calls == ["https://www.mfn.se/a/a/k"]


# --- Markörer ur befintlig data ------------------------------------------------------------------------------

def _item(**extra):
    base = {"id": 1, "currency": "SEK", "cash": 5e6, "cash_today": 4e6, "debt_due_12m": None,
            "available_credit": 0.0, "period_end": "2026-09-30"}
    return {**base, **extra}


def test_markers_from_existing_data_and_tiers():
    events = [
        {"type": "bridge_loan", "status": "completed", "gross_amount": 5e6, "currency": "SEK", "event_date": "2026-10-01"},
        {"type": "warrants", "status": "announced", "exercise_start": "2026-11-20", "exercise_end": "2026-12-04",
         "event_date": "2026-06-01"},
        {"type": "rights_issue", "status": "completed", "subscription_rate_pct": 62.0, "event_date": "2026-06-01"},
    ]
    titles = [{"title": "Bolagets VD lämnar sin tjänst", "published_at": "2026-10-20T08:00", "url": "u1"},
              {"title": "Bolaget avslutar avtal om likviditetsgaranti", "published_at": "2026-10-10T08:00", "url": "u2"}]
    signals = [{"kind": "egm_called", "quote": "Beslut om nyemission", "published_at": "2026-11-01", "url": "k",
                "timing": None, "source": "news"}]
    found = markers.for_company(_item(debt_due_12m=10e6), signals, events, {"date": "2026-11-25"}, titles, TODAY)
    keys = [m["key"] for m in found]
    assert keys == ["egm_called", "debt_due", "bridge_loan", "to_window", "weak_issue", "report_soon",
                    "mgmt_change", "lp_ended"]
    assert markers.need_level(found) == "capital"
    assert markers.need_level([m for m in found if m["tier"] != "capital"]) == "half"
    assert markers.need_level([m for m in found if m["tier"] == "context"]) is None


def test_markers_are_cleared_by_a_later_issue_and_credit_covers_debt():
    events = [{"type": "bridge_loan", "status": "completed", "gross_amount": 5e6, "event_date": "2026-09-01"},
              {"type": "directed_issue", "status": "completed", "gross_amount": 20e6, "event_date": "2026-10-01"}]
    found = markers.for_company(_item(debt_due_12m=10e6, available_credit=8e6), [], events, None, [], TODAY)
    assert found == []        # brygglånet återbetalt via emissionen; kredit + kassa täcker lånet


# --- Bransch -------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def complete(self, system, user):
        self.calls += 1
        return self.answers.pop(0), {"prompt_tokens": 100, "completion_tokens": 10}


def test_icb_import_classification_and_manual_override(tmp_path):
    legacy = tmp_path / "screener.db"
    source = sqlite3.connect(legacy)
    source.execute("CREATE TABLE securities (isin TEXT, metadata_json TEXT)")
    source.execute("INSERT INTO securities VALUES ('SE1', ?)", (json.dumps({"sector": "Health Care"}),))
    source.commit()
    source.close()
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin) VALUES (1, 'Medtech AB', 'SE1'), (2, 'NGM AB', 'SE2')")
        connection.execute("INSERT INTO reports(document_id, company_id, title, published_at) VALUES (9, 2, 'Q3', '2026-11-01')")
        connection.execute("INSERT INTO report_pages(document_id, page, text) VALUES (9, 1, 'Vi utvecklar spel för mobilen.')")
    assert sectors.import_icb(connection, legacy) == {"matched": 1, "missing": 1}
    client = FakeClient(['{"subsector": "medtech", "reason": "implantat"}', '{"subsector": "påhittad"}'])
    counts = sectors.classify(connection, client, limit=10)
    assert counts["classified"] == 1 and counts["failed"] == 1      # okänd nyckel godtas inte
    loaded = sectors.load(connection)
    assert loaded[1] == {"icb": "Health Care", "subsector": "medtech", "label": "Medtech", "group": "life_science"}
    sectors.set_manual(connection, 2, "gaming")
    client = FakeClient(['{"subsector": "software"}'])
    sectors.classify(connection, client, limit=10)
    assert client.calls == 0 and sectors.load(connection)[2]["subsector"] == "gaming"   # manuellt skrivs aldrig över
    with pytest.raises(ValueError):
        sectors.set_manual(connection, 2, "okänd")
