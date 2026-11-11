"""Tester för egen MFN-insamling: tolkning, klassificering, id, dubbletter, körning och jämförelse."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from radar import collect, compare, mfn, scheduler
from radar.classify import classify
from radar.db import connect, next_document_id
from radar.importer import import_legacy
from radar.settings import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "mfn_issuer_page.html"
BASE = "https://www.mfn.se/a/actic-group/"
REPORT_URL = BASE + "delarsrapport-juli-september-2026"


@pytest.fixture()
def page_items() -> list[mfn.MfnItem]:
    return mfn.parse_page(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "files",
                   legacy_db_path=tmp_path / "screener.db")


def add_company(connection, company_id=1, name="Actic Group", slug="actic-group") -> None:
    with connection:
        connection.execute("INSERT INTO companies(id, name, mfn_slug) VALUES (?,?,?)", (company_id, name, slug))


# --- Tolkning av sidan ---------------------------------------------------------

def test_parse_real_page_structure(page_items):
    assert len(page_items) == 13
    first = page_items[0]
    assert first.title == "Actic tecknar avtal om driften av Gustavsvik i Örebro – övertagande i januari 2027"
    assert first.published_at == "2026-11-04T07:30:00+01:00"      # vintertid
    assert first.url == BASE + "actic-tecknar-avtal-om-driften-av-gustavsvik-i-orebro-overtagande-i-januari-2027"
    assert first.language == "sv" and first.category is None
    assert first.pdf_url and first.pdf_url.startswith("https://storage.mfn.se/") and first.pdf_url.endswith(".pdf")
    summer = next(item for item in page_items if item.url.endswith("delarsrapport-april-juni-2026"))
    assert summer.published_at == "2026-08-25T07:40:00+02:00"      # sommartid


def test_parse_handles_missing_parts_and_category():
    html = """<div class="short-item" id="a"><span class="compressed-date">2026-03-29</span>
        <span class="compressed-time">01:59:00</span><span class="category">Rapport</span>
        <a class="item-link" href="/a/x/en-post" title="Interim report Q1">Interim report Q1</a>
        <a class="attachment-icon" href="https://storage.mfn.se/1/a.docx"></a></div>
        <div class="short-item" id="b"><span class="compressed-date">2026-03-29</span>
        <a class="item-link" href="/a/x/saknar-tid" title="Utan tid">x</a></div>"""
    items = mfn.parse_page(html)
    assert len(items) == 1
    assert items[0].published_at == "2026-03-29T01:59:00+01:00"     # före omställningen kl 02
    assert items[0].category == "Rapport" and items[0].language == "en" and items[0].pdf_url is None


def test_stockholm_offset_around_dst_changes():
    assert mfn.stockholm_iso(datetime(2026, 3, 29, 3, 0)).endswith("+02:00")
    assert mfn.stockholm_iso(datetime(2026, 10, 25, 2, 59)).endswith("+02:00")
    assert mfn.stockholm_iso(datetime(2026, 10, 25, 3, 0)).endswith("+01:00")


def test_slug_and_url_helpers():
    assert mfn.slug_from_url("https://www.mfn.se/a/actic-group/delarsrapport-2026") == "actic-group"
    assert mfn.slug_from_url("https://mfn.se/all/a/aira") == "aira"
    assert mfn.slug_from_url("https://mfn.se/a/11") is None
    assert mfn.slug_from_url("https://example.com/a/x/y") is None and mfn.slug_from_url(None) is None
    assert mfn.normalize_url("http://mfn.se/a/x/y/?z=1#f") == "http://www.mfn.se/a/x/y"
    assert mfn.page_url("actic-group") == "https://www.mfn.se/all/a/actic-group"
    with pytest.raises(mfn.FetchError):
        mfn.page_url("../etc")


def test_fetch_items_uses_polite_rules(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(mfn, "fetch_html", lambda url, s, t=None: seen.append(url) or FIXTURE.read_text("utf-8"))
    assert len(mfn.fetch_items("actic-group", settings)) == 13
    assert seen == ["https://www.mfn.se/all/a/actic-group"]
    monkeypatch.setattr(mfn, "fetch_html", lambda *a, **k: "<html></html>")
    with pytest.raises(mfn.FetchError):
        mfn.fetch_items("actic-group", settings)


def test_only_https_and_allowed_hosts(settings):
    with pytest.raises(mfn.FetchError):
        mfn.fetch_html("http://www.mfn.se/all/a/x", settings)       # bara https
    with pytest.raises(mfn.FetchError):
        mfn.fetch_html("https://example.com/all/a/x", settings)


# --- Klassificering ------------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("Delårsrapport juli - september 2026", "report"),
    ("Delårsrapport januari – mars 2024", "report"),
    ("BOKSLUTSKOMMUNIKÉ 2024", "report"),
    ("Bokslutskommuniké 2025", "report"),
    ("Actic Groups årsredovisning och hållbarhetsrapport 2025", "report"),
    ("Halvårsrapport januari-juni 2026", "report"),
    ("Interim report January - September 2026", "report"),
    ("Year-end report 2025", "report"),
    ("Half-year report 2026", "report"),
    ("Q3 2026 report: strong growth", "report"),
    ("Kommuniké från årsstämma den 12 maj 2026 i Actic Group AB", "other"),
    ("Årsstämman godkände årsredovisningen för 2025", "other"),
    ("KALLELSE TILL ÅRSSTÄMMA I ACTIC GROUP AB", "other"),
    ("Inbjudan till presentation av delårsrapport för tredje kvartalet", "calendar"),
    ("Invitation to webcast on the interim report Q3 2026", "calendar"),
    ("Foo publicerar delårsrapport för tredje kvartalet den 24 oktober", "calendar"),
    ("Foo will publish its year-end report on 14 February 2027", "calendar"),
    ("Ändrat datum för publicering av delårsrapport", "calendar"),
    ("Finansiell kalender 2027", "calendar"),
    ("Bolaget genomför en företrädesemission om 50 MSEK", "financing"),
    ("Actic Group AB säkrar långsiktig finansiering med DNB för fortsatt tillväxt", "financing"),
    ("Foo announces directed share issue and bridge loan", "financing"),
    ("Foo tecknar låneavtal om 20 MSEK", "financing"),
    ("Actic Groups VD genomför köp av aktier", "other"),
    ("Ökat antal aktier och röster i Actic Group AB", "other"),
    ("Actic öppnar sitt åttonde gym i Uppsala", "other"),
])
def test_classification_table(title, expected):
    assert classify(title) == expected


def test_classification_uses_mfn_category():
    assert classify("Foo", "Financial calendar") == "calendar"


def test_real_page_titles_classify_sensibly(page_items):
    kinds = {item.url.rsplit("/", 1)[1]: classify(item.title) for item in page_items}
    assert kinds["delarsrapport-juli-september-2026"] == "report"
    assert kinds["actic-groups-arsredovisning-och-hallbarhetsrapport-2025"] == "report"
    assert kinds["kommunike-fran-arsstamma-den-12-maj-2026-i-actic-group-ab"] == "other"
    assert sum(kind == "report" for kind in kinds.values()) == 4


# --- Id och dubbletter ---------------------------------------------------------

def test_new_ids_start_above_legacy_range(settings):
    connection = connect(settings.db_path)
    assert next_document_id(connection) == 10_000_001
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'A')")
        connection.execute("INSERT INTO reports(document_id, company_id, title) VALUES (123456, 1, 'r')")
    assert next_document_id(connection) == 10_000_001
    with connection:
        connection.execute("INSERT INTO news(document_id, company_id, title) VALUES (10000042, 1, 'n')")
    assert next_document_id(connection) == 10_000_043


def test_parallel_inserts_never_share_an_id(settings, page_items):
    add_company(connect(settings.db_path))
    errors: list[Exception] = []

    def worker(offset: int) -> None:
        connection = connect(settings.db_path)
        try:
            for item in page_items[offset::4]:
                collect.insert_item(connection, 1, item, "news")
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = [row[0] for row in connect(settings.db_path).execute("SELECT document_id FROM news")]
    assert not errors and len(ids) == 13 and len(set(ids)) == 13 and min(ids) > 10_000_000


def test_old_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, isin TEXT,
            symbol TEXT, market TEXT, segment TEXT);
            CREATE TABLE reports (document_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, title TEXT NOT NULL,
            published_at TEXT, pdf_url TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT, pdf_path TEXT,
            pdf_sha256 TEXT, page_count INTEGER, updated_at TEXT);
            CREATE TABLE news (document_id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, title TEXT NOT NULL,
            published_at TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'new', error TEXT, text TEXT,
            tokens INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
            INSERT INTO companies(id, name) VALUES (1, 'A');""")
    connection = connect(path)
    for table, column in (("companies", "mfn_slug"), ("reports", "source_url"), ("news", "source_url")):
        assert column in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    connect(path)  # en andra anslutning ska inte krocka


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "screener.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE issuers (id INTEGER PRIMARY KEY, canonical_name TEXT);
            CREATE TABLE securities (id INTEGER PRIMARY KEY, issuer_id INT, isin TEXT, symbol TEXT,
                                     market TEXT, segment TEXT, active INT);
            CREATE TABLE documents (id INTEGER PRIMARY KEY, issuer_id INT, title TEXT, published_at TEXT,
                                    profile_category TEXT, canonical_url TEXT);
            CREATE TABLE attachments (id INTEGER PRIMARY KEY, document_id INT, url TEXT);
            INSERT INTO issuers VALUES (1, 'Actic Group'), (2, 'Utan Mfn');
            INSERT INTO documents VALUES (10, 1, 'Delårsrapport juli - september 2026',
                '2026-10-23T07:40:00+02:00', 'report', 'https://mfn.se/a/actic-group/delarsrapport-juli-september-2026');
            INSERT INTO documents VALUES (11, 1, 'Actic Groups VD genomför köp av aktier',
                '2026-10-26T16:30:00+01:00', 'other', NULL);
            INSERT INTO documents VALUES (12, 1, 'Saknas på sidan', '2026-10-27T10:00:00+01:00', 'other',
                'https://www.mfn.se/a/actic-group/saknas-pa-sidan');
            INSERT INTO documents VALUES (13, 2, 'Något', '2026-10-27T10:00:00+01:00', 'other', NULL);
            INSERT INTO attachments VALUES (1, 10, 'https://storage.mfn.se/x/rapport.pdf');
        """)
    return path


def test_import_seeds_slug_and_source_url_and_is_idempotent(settings, legacy_db):
    connection = connect(settings.db_path)
    first = import_legacy(connection, legacy_db)
    assert first["mfn_slugs"] == 1
    assert import_legacy(connection, legacy_db) == first
    slugs = dict(connection.execute("SELECT id, mfn_slug FROM companies").fetchall())
    assert slugs == {1: "actic-group", 2: None}
    assert connection.execute("SELECT source_url FROM reports WHERE document_id=10").fetchone()[0] == REPORT_URL
    assert connection.execute("SELECT COUNT(*) FROM news WHERE source_url IS NOT NULL").fetchone()[0] == 1


def test_collector_does_not_duplicate_imported_rows(settings, legacy_db, page_items, monkeypatch):
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: page_items)
    counts = collect.collect_company(connection, settings, {"id": 1, "name": "Actic", "mfn_slug": "actic-group"})
    assert counts["fetched"] == 13
    assert counts["new_reports"] == 3        # de fyra rapporterna minus den importerade
    # VD-köpet (id 11) har ingen länk men matchar på bolag + rubrik + dag
    assert connection.execute("SELECT COUNT(*) FROM news WHERE title LIKE '%VD genomför köp%'").fetchone()[0] == 1
    ids = [row[0] for row in connection.execute("SELECT document_id FROM reports ORDER BY document_id")]
    assert ids[0] == 10 and ids[1] > 10_000_000


# --- Körning -------------------------------------------------------------------

def test_collect_all_counts_isolates_failures_and_skips_missing_slugs(settings, page_items, monkeypatch):
    connection = connect(settings.db_path)
    add_company(connection, 1, "Bra AB", "bra-ab")
    add_company(connection, 2, "Trasigt AB", "trasigt-ab")
    add_company(connection, 3, "Utan slug", None)
    add_company(connection, 4, "Bra Två AB", "bra-tva")

    def fake(slug, settings, throttle=None):
        if slug == "trasigt-ab":
            raise mfn.FetchError("HTTP 500")
        return [replace(item, url=item.url.replace("actic-group", slug)) for item in page_items]
    monkeypatch.setattr(mfn, "fetch_items", fake)
    logged: list[str] = []
    counts = collect.collect_all(settings, workers=3, log=logged.append)
    assert counts["companies"] == 3 and counts["fetched"] == 26 and counts["failed"] == 1
    assert counts["skipped_no_slug"] == 1
    assert counts["new_reports"] == 8 and counts["new_news"] == 2 * (13 - 4 - _calendar_count(page_items))
    assert any("Trasigt AB" in line for line in logged)
    again = collect.collect_all(settings, workers=2, log=lambda _l: None)
    assert (again["new_reports"], again["new_news"], again["failed"]) == (0, 0, 1)
    row = connect(settings.db_path).execute(
        "SELECT status, pdf_url, source_url FROM reports ORDER BY document_id").fetchone()
    assert row["status"] == "new" and row["pdf_url"].endswith(".pdf") and row["source_url"].startswith(BASE[:20])


def _calendar_count(items) -> int:
    return sum(classify(item.title) == "calendar" for item in items)


def test_calendar_items_are_not_stored(settings, monkeypatch):
    connection = connect(settings.db_path)
    add_company(connection)
    invite = mfn.MfnItem("Inbjudan till presentation av delårsrapporten", "2026-10-20T08:00:00+02:00",
                         BASE + "inbjudan", "sv")
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: [invite])
    counts = collect.collect_company(connection, settings, {"id": 1, "name": "A", "mfn_slug": "actic-group"})
    assert counts == {"fetched": 1, "new_reports": 0, "new_news": 0, "calendar": 1, "probed_reports": 0,
                      "retry_reports": 0}


def test_dry_run_writes_nothing_to_radar_db(settings, legacy_db, page_items, monkeypatch):
    connection = connect(settings.db_path)
    import_legacy(connection, legacy_db)
    connection.close()
    before = sqlite3.connect(settings.db_path).execute("SELECT COUNT(*) FROM news").fetchone()[0]
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: page_items)
    counts = collect.collect_all(settings, dry_run=True, log=lambda _l: None)
    assert counts["new_news"] > 0
    assert sqlite3.connect(settings.db_path).execute("SELECT COUNT(*) FROM news").fetchone()[0] == before


def test_dry_run_without_radar_db_creates_nothing(settings, legacy_db, page_items, monkeypatch):
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: page_items)
    collect.collect_all(settings, dry_run=True, log=lambda _l: None)
    assert not settings.db_path.exists()


# --- Jämförelse mot gamla databasen --------------------------------------------

def test_compare_legacy_math(settings, legacy_db, page_items, monkeypatch):
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: page_items)
    result = compare.compare_legacy(settings, 30, today=date(2026, 11, 5), log=lambda _l: None)
    assert result["legacy_documents"] == 4
    assert result["reports"] == {"total": 1, "matched": 1, "percent": 100.0, "classified_as_report": 1}
    assert result["other"]["total"] == 3 and result["other"]["matched"] == 1     # bara VD-köpet finns på sidan
    assert result["matched"] == 2 and result["matched_percent"] == 50.0
    assert {miss["id"] for miss in result["misses"]} == {12, 13}
    assert result["companies_fetched"] == 1 and result["companies_without_slug"] == 1
    assert not settings.db_path.exists()                                          # inget skrivs till radar.db


def test_compare_counts_fetch_failures(settings, legacy_db, monkeypatch):
    def broken(*a, **k):
        raise mfn.FetchError("nere")
    monkeypatch.setattr(mfn, "fetch_items", broken)
    result = compare.compare_legacy(settings, 30, today=date(2026, 11, 5), log=lambda _l: None)
    assert result["companies_failed"] == 1 and result["matched"] == 0


# --- Schemaläggaren ------------------------------------------------------------

@pytest.mark.parametrize("source, expected", [("legacy", "import"), ("mfn", "collect")])
def test_scheduler_switch(settings, legacy_db, monkeypatch, source, expected):
    called = []
    monkeypatch.setattr(scheduler, "collect_all", lambda *a, **k: called.append("collect") or {"companies": 0})
    monkeypatch.setattr(scheduler, "import_legacy", lambda *a, **k: called.append("import") or {"companies": 0})
    monkeypatch.setattr(scheduler, "run_latest", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "refresh_many", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "run_news", lambda *a, **k: {})
    summary = scheduler.run_cycle(replace(settings, collect_source=source), None, report_budget=1, news_budget=0,
                                  log=lambda _l: None)
    assert called == [expected] and expected in summary


def test_collect_source_defaults_to_mfn(monkeypatch):
    monkeypatch.delenv("RADAR_COLLECT_SOURCE", raising=False)
    assert Settings.from_env().collect_source == "mfn"
    monkeypatch.setenv("RADAR_COLLECT_SOURCE", "LEGACY")
    assert Settings.from_env().collect_source == "legacy"
    monkeypatch.setenv("RADAR_COLLECT_SOURCE", "något annat")
    assert Settings.from_env().collect_source == "mfn"


def test_slug_from_url_accepts_every_mfn_channel():
    from radar.mfn import slug_from_url

    base = "https://www.mfn.se"
    assert slug_from_url(f"{base}/a/senzime/post-title-1a2b3c4d") == "senzime"
    assert slug_from_url(f"{base}/all/a/senzime") == "senzime"
    for channel in ("cis", "one", "beq", "eqs", "nir", "unk", "ind", "ob", "notc"):
        assert slug_from_url(f"{base}/{channel}/a/rottneros/post-title-4599a6a5") == "rottneros", channel
    assert slug_from_url(f"{base}/cis/a/rottneros") is None  # kanalsida utan post är ingen bolagslänk
    assert slug_from_url(f"{base}/other/rottneros/post") is None
    assert slug_from_url("https://example.com/a/x/y") is None


def test_definite_form_quarter_report_titles_are_reports():
    from radar.classify import classify

    for title in ("Oncopeptides publicerar rapporten för det tredje kvartalet 2026",
                  "Stille AB (publ) redovisar idag rapporten för det tredje kvartalet 2026."):
        assert str(getattr(classify(title), "kind", classify(title))) == "report", title
    for title in ("Cantargia bjuder in till presentation av delårsrapport för Q3 2026",
                  "Observit AB tidigarelägger publicering av kvartalsrapporten för tredje kvartalet 2026"):
        assert str(getattr(classify(title), "kind", classify(title))) == "calendar", title


def test_report_recognised_from_attachment_filename():
    # Rubriker i nyhetsform: bara bilagans filnamn visar att det är en kvartalsrapport.
    assert classify("Agtiras starkaste kvartal – hög organisk tillväxt", None,
                    ("https://mfn.se/a/agtira/agtira-kvartalsrapport-q1-2026.pdf",)) == "report"
    assert classify("Alligator Bioscience AB reports financial results for the period 1 January – 30 September 2026",
                    None, ("https://mfn.se/a/x/2026-q3-en-vf.pdf",)) == "report"
    assert classify("Adverty når positiv EBITDA i Q1 och stärker marginalerna", None,
                    ("https://mfn.se/a/adverty/adverty-q1-2026-sve.pdf",)) == "report"
    # Stämmor, kallelser och inbjudningar blir aldrig rapporter, även med rapport som bilaga.
    assert classify("Kallelse till årsstämma i Agtira AB", None, ("https://x/arsredovisning-2025.pdf",)) != "report"
    assert classify("Inbjudan till rapportpresentation den 22 oktober 2026", None, ("https://x/q3-2026.pdf",)) == "calendar"
    assert classify("Agtira säkrar finansiering om cirka 35 MSEK", None, ("https://x/pressmeddelande.pdf",)) == "financing"


def test_report_saved_as_news_is_added_as_report(tmp_path):
    from radar.collect import insert_item
    from radar.db import connect
    from radar.mfn import MfnItem

    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name) VALUES (1, 'Agtira')")
    item = MfnItem("Agtiras starkaste kvartal", "2026-05-13T08:00:00+02:00",
                   "https://mfn.se/all/a/agtira/agtiras-starkaste-kvartal", "sv",
                   attachments=("https://x/agtira-kvartalsrapport-q1-2026.pdf",))
    assert insert_item(connection, 1, item, "news")
    assert not insert_item(connection, 1, item, "news")      # samma nyhet läggs inte in två gånger
    assert insert_item(connection, 1, item, "reports")       # men rapporten läggs till
    assert not insert_item(connection, 1, item, "reports")
    assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
