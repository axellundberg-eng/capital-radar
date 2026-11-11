"""Stämmobeslut om emission och uttalade planer att ta in kapital (plans.py)."""

from __future__ import annotations

from radar import plans
from radar.db import connect


def test_stamma_issue_is_found_but_not_authorizations_or_incentive_programs():
    rights = ("Stämman beslutade att godkänna styrelsens beslut från den 8 juni 2026 om en nyemission av högst "
              "7 654 999 aktier med företrädesrätt för bolagets befintliga aktieägare.")
    assert plans.find_stamma_issue(rights)["kind"] == "stamma_issue"
    english = ("The general meeting resolved to approve the Board of Directors' resolution of 17 July 2026 on a "
               "rights issue of a maximum of 36,383,904 shares.")
    assert plans.find_stamma_issue(english) is not None
    mandate = ("Stämman beslutade om bemyndigande för styrelsen att besluta om nyemission av aktier med eller "
               "utan avvikelse från aktieägarnas företrädesrätt.")
    assert plans.find_stamma_issue(mandate) is None
    incentive = ("Stämman beslutade om en riktad emission av aktier till ledande befattningshavare inom ramen för "
                 "ett incitamentsprogram.")
    assert plans.find_stamma_issue(incentive) is None
    # Climeon: stämman godkände den del av en redan genomförd riktad emission som gick till ledningen
    # (avstavat "befatt-ningshavare" i texten).
    climeon = ("Stämman beslutade att besluta om en nyemission omfattande sammanlagt högst 539 500 B-aktier "
               "riktad till ledande befatt-ningshavare som omfattas av 16 kap aktiebolagslagen. Samtliga aktier har "
               "tecknats och tilldelats i samband med bolagsstämman.")
    assert plans.find_stamma_issue(climeon) is None


def test_plan_with_timing_beats_vaguer_sentences():
    text = ("Styrelsen utvärderar löpande olika finansieringsalternativ. Bolaget avser att genomföra en "
            "företrädesemission under första kvartalet 2027 för att finansiera fas II-studien.")
    found = plans.find_plan(text)
    assert found["kind"] == "plan" and found["timing"] == "under första kvartalet 2027"
    assert plans.find_plan("Styrelsen utvärderar löpande olika finansieringsalternativ.")["kind"] == "evaluating"


def test_plan_ignores_accounting_text_completed_issues_hedges_and_calendars():
    for text in ("Kassaflödet avser kapitaltillskott till dotterbolaget.",          # "avser" = gäller
                 "Bolaget genomförde en nyemission om 20 MSEK under 2026.",          # redan genomförd
                 "Eftersom bolaget inte har något behov av att ta in nytt kapital sker ingen emission.",
                 "The company may need to raise additional capital in the future.",  # riskavsnitt
                 # Rapportkalendern får inte ge en falsk tidpunkt åt en plan i samma mening.
                 "The company plans to publish its Quarterly Report Q3 2026 and raise capital later."):
        assert plans.find_plan(text) is None, text


def _seed(connection):
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'Bolag A'), (2, 'Bolag B')")
        for doc, company in ((10, 1), (20, 2)):
            connection.execute("INSERT INTO reports(document_id, company_id, title, published_at, pdf_url) "
                               "VALUES (?, ?, 'Delårsrapport', '2026-08-20', 'https://example.test/r.pdf')",
                               (doc, company))
            connection.execute("INSERT INTO runway_results(company_id, document_id, computed_at, result_json) "
                               "VALUES (?, ?, '2026-08-21', '{}')", (company, doc))
        connection.execute("INSERT INTO report_pages(document_id, page, text) VALUES (10, 3, ?)",
                           ("Bolaget planerar att ta in nytt kapital under hösten 2026.",))
        connection.execute("INSERT INTO report_pages(document_id, page, text) VALUES (20, 2, 'Inget om finansiering.')")
        connection.execute("INSERT INTO news(document_id, company_id, title, published_at, url, text) VALUES "
                           "(30, 2, 'Kommuniké från extra bolagsstämma', '2026-09-15', 'https://example.test/n', ?)",
                           ("Bolagsstämman beslutade om en företrädesemission av units.",))


def test_run_and_active_signal_is_cleared_by_a_later_completed_issue(tmp_path):
    connection = connect(tmp_path / "radar.db")
    _seed(connection)
    counts = plans.run(connection, today="2026-11-10")
    assert counts["plan"] == 1 and counts["stamma_issue"] == 1 and counts["companies"] == 2
    signals = plans.active(connection, {}, "2026-11-10")
    assert signals[1]["kind"] == "plan" and signals[1]["timing"] == "under hösten 2026"
    assert signals[2]["kind"] == "stamma_issue" and signals[2]["url"] == "https://example.test/n"
    # Bolag B genomför emissionen efter stämman: signalen släcks. Ett lån räknas inte.
    history = {2: [{"type": "rights_issue", "status": "completed", "event_date": "2026-10-10"}],
               1: [{"type": "loan", "status": "completed", "event_date": "2026-09-01"}]}
    assert set(plans.active(connection, history, "2026-11-10")) == {1}
    # Stämman godkände ett styrelsebeslut som registrerats en månad före stämman: också genomfört.
    earlier = {2: [{"type": "rights_issue", "status": "completed", "event_date": "2026-08-15"}]}
    assert set(plans.active(connection, earlier, "2026-11-10")) == {1}
    # Stämmobeslut gäller i 120 dagar, planer i 270.
    assert set(plans.active(connection, {}, "2027-02-01")) == {1}
    assert plans.active(connection, {}, "2027-08-01") == {}
