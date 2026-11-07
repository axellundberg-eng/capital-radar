"""MFN-slugar: normalisering, matchning, torrkörning, skrivning och CSV. Inga nätverksanrop."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from radar import slugs
from radar.db import connect
from radar.settings import Settings

# Trimmat riktigt svar från GET https://www.mfn.se/search/companies?limit=5&query=Bambuser (verifierat).
BAMBUSER_RESPONSE = """
[{"entity_id":"e1042451-924d-42e9-af3c-1fdb40842bae","slug":"bambuser","slugs":["bambuser"],
  "name":"Bambuser AB","isins":["SE0027099326"],"local_refs":["SE:556731-3126"],
  "tickers":["FNSE:BUSER","CAPA:BUSERs"]}]
"""


def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "radar.db", files_path=tmp_path / "files", legacy_db_path=tmp_path / "old.db",
        deepseek_model="test", deepseek_base_url="https://example.invalid", http_timeout=30.0,
        user_agent="TestAgent/1.0")


def hit(slug: str, name: str, isin: str | None = None, entity: str | None = None) -> dict:
    """Syntetisk träff med samma form som MFN:s svar."""
    return {"entity_id": entity or f"entity-{slug}", "slug": slug, "slugs": [slug], "name": name,
            "isins": [isin] if isin else [], "local_refs": [], "tickers": []}


class Row(dict):
    """Liknar sqlite3.Row för match(): åtkomst med nyckel."""


def row(name: str, isin: str | None = None, company_id: int = 1) -> Row:
    return Row(id=company_id, name=name, isin=isin)


def fake_fetch(table: dict[str, list[dict]]):
    """Fetch som tar en URL och svarar utifrån query-parametern, utan nätverk."""
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        query = url.split("query=", 1)[1].replace("+", " ")
        return table.get(query, [])

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def make_db(tmp_path: Path, companies: list[tuple]):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.executemany(
            "INSERT INTO companies (id, name, isin, mfn_slug) VALUES (?, ?, ?, ?)", companies)
    return connection


# --- normalisering -------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Bambuser AB", "bambuser"),
    ("Smart Eye AB (publ)", "smart eye"),
    ("  Kancera   AB ", "kancera"),
    ("Cortus-Energy, AB", "cortus energy"),
    ("AB Volvo", "volvo"),
    ("Hexagon Holding", "hexagon holding"),        # holding behålls medvetet
    ("Abliva AB", "abliva"),                        # "ab" bara som eget ord
    ("Sivers IMA Holding AB (publ)", "sivers ima holding"),
    ("Åhléns AB", "åhléns"),
])
def test_normalize(raw: str, expected: str) -> None:
    assert slugs.normalize(raw) == expected


# --- kandidater ----------------------------------------------------------------------------

def test_candidates_uses_injected_fetch_and_builds_url(tmp_path: Path) -> None:
    fetch = fake_fetch({"Bambuser AB": json.loads(BAMBUSER_RESPONSE)})
    hits = slugs.candidates("Bambuser AB", settings(tmp_path), fetch=fetch)
    assert hits[0]["slug"] == "bambuser"
    assert fetch.seen[0].startswith("https://www.mfn.se/search/companies?")
    assert "limit=5" in fetch.seen[0] and "query=Bambuser+AB" in fetch.seen[0]


def test_candidates_sends_user_agent_and_bounded_timeout(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _limit=-1):
            return BAMBUSER_RESPONSE.encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["agent"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(slugs, "urlopen", fake_urlopen)
    hits = slugs.candidates("Bambuser AB", settings(tmp_path))
    assert hits[0]["slug"] == "bambuser"
    assert captured["agent"] == "TestAgent/1.0"
    assert 0 < captured["timeout"] <= 60


def test_candidates_ignores_non_list_payload(tmp_path: Path) -> None:
    assert slugs.candidates("X", settings(tmp_path), fetch=lambda url: {"error": "x"}) == []


# --- matchning -----------------------------------------------------------------------------

def test_isin_match_beats_exact_name(tmp_path: Path) -> None:
    results = [
        hit("bolaget-namn", "Bambuser AB", isin="SE0000000001"),        # namn exakt, fel ISIN
        hit("bambuser", "Bambuser Holding AB", isin="SE0027099326"),    # ISIN exakt
    ]
    assert slugs.match(row("Bambuser AB", "SE0027099326"), results) == ("bambuser", "isin", 1.0)


def test_isin_beats_close_name(tmp_path: Path) -> None:
    results = [hit("smart-eyes", "Smart Eyes AB", isin="SE0000000002"),
               hit("smart-eye", "Smart Eye AB", isin="SE0011111111")]
    assert slugs.match(row("Smart Eye AB", "SE0011111111"), results)[:2] == ("smart-eye", "isin")


def test_name_exact_without_isin(tmp_path: Path) -> None:
    results = [hit("kancera-x", "Kancera AB (publ)")]
    assert slugs.match(row("Kancera AB"), results) == ("kancera-x", "name_exact", 1.0)


def test_name_close_at_threshold(tmp_path: Path) -> None:
    results = [hit("smart-eye", "Smart Eyes AB")]
    slug, method, score = slugs.match(row("Smart Eye AB"), results)
    assert (slug, method) == ("smart-eye", "name_close")
    assert score >= 0.9


def test_no_match_below_threshold(tmp_path: Path) -> None:
    assert slugs.match(row("Smart Eye AB"), [hit("hexagon", "Hexagon AB")]) is None


def test_tie_between_two_entities_gives_no_match(tmp_path: Path) -> None:
    # Två olika bolag med samma normaliserade namn: ingen matchning.
    results = [hit("a-one", "Nordic Tech AB"), hit("a-two", "Nordic Tech AB (publ)")]
    assert slugs.match(row("Nordic Tech AB"), results) is None


def test_tie_on_same_isin_gives_no_match(tmp_path: Path) -> None:
    results = [hit("first", "Foo AB", isin="SE0000000003"), hit("second", "Bar AB", isin="SE0000000003")]
    assert slugs.match(row("Baz AB", "SE0000000003"), results) is None


def test_tie_on_close_names_gives_no_match(tmp_path: Path) -> None:
    results = [hit("eye-a", "Smart Eyes AB"), hit("eye-b", "Smart Eyes AB")]
    # Samma namn men olika entiteter: oavgjort.
    assert slugs.match(row("Smart Eye AB"), results) is None


def test_same_entity_with_two_slugs_is_not_a_tie(tmp_path: Path) -> None:
    # Sivers-liknande fall: samma entity_id med två slugar är ett bolag.
    results = [hit("sivers-ima-holding", "Sivers IMA Holding AB", entity="sivers"),
               hit("sivers-semiconductors", "Sivers IMA Holding AB", entity="sivers")]
    assert slugs.match(row("Sivers IMA Holding AB"), results)[:2] == ("sivers-ima-holding", "name_exact")


def test_empty_results_give_no_match(tmp_path: Path) -> None:
    assert slugs.match(row("Kancera AB"), []) is None


# --- torrkörning, skrivning, CSV -----------------------------------------------------------

def test_dry_run_writes_nothing_and_proposes(tmp_path: Path) -> None:
    connection = make_db(tmp_path, [
        (1, "Bambuser AB", "SE0027099326", None),
        (2, "Kancera AB", None, None),
        (3, "Smart Eye AB", None, None),
        (4, "Redan AB", None, "redan"),          # har slug, ska inte med
    ])
    before = connection.execute("SELECT * FROM companies ORDER BY id").fetchall()
    table = {
        "Bambuser AB": json.loads(BAMBUSER_RESPONSE),
        "Kancera AB": [],
        "Smart Eye AB": [hit("smart-eyes", "Smart Eyes AB")],
    }
    sleeps: list[float] = []
    logs: list[str] = []
    proposals = slugs.dry_run(connection, settings(tmp_path), fetch=fake_fetch(table),
                              sleep=sleeps.append, log=logs.append)

    assert [p["id"] for p in proposals] == [1, 2, 3]
    assert proposals[0]["slug"] == "bambuser" and proposals[0]["method"] == "isin"
    assert proposals[0]["candidate_names"] == ["Bambuser AB"]
    assert proposals[1]["method"] is None and proposals[1]["slug"] is None
    assert proposals[2]["method"] == "name_close" and proposals[2]["candidate_names"] == ["Smart Eyes AB"]
    assert len(sleeps) == 2 and all(pause == 1.5 for pause in sleeps)
    assert len(logs) == 3
    after = connection.execute("SELECT * FROM companies ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_apply_only_allowed_methods_and_never_overwrites(tmp_path: Path) -> None:
    connection = make_db(tmp_path, [
        (1, "Bambuser AB", "SE0027099326", None),
        (2, "Kancera AB", None, None),
        (3, "Smart Eye AB", None, None),
        (4, "Gammal AB", None, "gammal"),
    ])
    proposals = [
        {"id": 1, "slug": "bambuser", "method": "isin"},
        {"id": 2, "slug": "kancera-x", "method": "name_exact"},
        {"id": 3, "slug": "smart-eyes", "method": "name_close"},   # ej tillåten som standard
        {"id": 4, "slug": "ny-slug", "method": "isin"},            # får inte skriva över
        {"id": 2, "slug": None, "method": None},
    ]
    assert slugs.apply(connection, proposals) == 2
    values = dict(connection.execute("SELECT id, mfn_slug FROM companies").fetchall())
    assert values == {1: "bambuser", 2: "kancera-x", 3: None, 4: "gammal"}


def test_apply_with_explicit_methods_includes_close(tmp_path: Path) -> None:
    connection = make_db(tmp_path, [(3, "Smart Eye AB", None, None)])
    proposals = [{"id": 3, "slug": "smart-eyes", "method": "name_close"}]
    assert slugs.apply(connection, proposals, methods=("name_close",)) == 1
    assert connection.execute("SELECT mfn_slug FROM companies WHERE id=3").fetchone()[0] == "smart-eyes"


def test_write_csv(tmp_path: Path) -> None:
    proposals = [{"id": 1, "name": "Bambuser AB", "isin": "SE0027099326", "slug": "bambuser",
                  "method": "isin", "score": 1.0, "candidate_names": ["Bambuser AB", "Annat AB"],
                  "error": None}]
    target = slugs.write_csv(proposals, tmp_path / "out" / "slugar.csv")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["slug"] == "bambuser"
    assert rows[0]["candidate_names"] == "Bambuser AB | Annat AB"
