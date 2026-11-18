"""Lägg till ett bolag som saknas i universumet."""

from __future__ import annotations

import pytest

from radar import addcompany
from radar.db import connect


def test_add_gives_a_high_id_and_never_duplicates(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin, mfn_slug) VALUES (5, 'Gammalt', 'SE1', 'gammalt')")
    first, created = addcompany.add(connection, "Foremont-Group", "Foremont Group", market="Spotlight")
    assert created and first == addcompany.FIRST_ID
    assert addcompany.add(connection, "foremont-group", "Foremont Group") == (first, False)       # samma slug
    assert addcompany.add(connection, "annat-namn", "Annat", isin="SE1") == (5, False)             # samma ISIN
    second, _ = addcompany.add(connection, "tredje-bolaget", "Tredje")
    assert second == first + 1
    assert connection.execute("SELECT mfn_slug, market FROM companies WHERE id=?", (first,)).fetchone()[:] == ("foremont-group", "Spotlight")
    with pytest.raises(ValueError):
        addcompany.add(connection, "../fel slug", "X")


def test_market_is_read_from_the_mfn_page():
    assert addcompany.detect_market("... Spotlight Stock Market ...") == "Spotlight"
    assert addcompany.detect_market("ingenting") is None


def test_cli_add_runs_without_a_model_key(tmp_path, monkeypatch, capsys):
    """Kommandot ska fungera även utan DeepSeek-nyckel (AI-stegen hoppas då över)."""
    import json

    from radar import __main__ as cli, mfn

    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    monkeypatch.setenv("RADAR_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(cli, "_client", lambda settings: None)
    monkeypatch.setattr(mfn, "fetch_html", lambda *a, **k: "<html>Spotlight</html>")
    monkeypatch.setattr(mfn, "fetch_items", lambda *a, **k: [])
    assert cli.main(["add", "--slug", "foremont-group", "--name", "Foremont Group"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created"] is True and out["company_id"] >= addcompany.FIRST_ID and "hoppades" in out["ai"]


def test_existing_company_gets_missing_details_but_nothing_is_overwritten(tmp_path):
    connection = connect(tmp_path / "radar.db")
    with connection:
        connection.execute("INSERT INTO companies(id, name, isin, symbol, market) VALUES (505, 'Wonderboo', 'SE9', 'WONDR', 'NGM')")
        connection.execute("INSERT INTO companies(id, name, mfn_slug) VALUES (6, 'Sustainion', 'sustainion-group')")
    assert addcompany.add(connection, "wonderboo", "Wonderboo Holding", isin="SE9", symbol="ANNAT") == (505, False)
    assert connection.execute("SELECT mfn_slug, symbol FROM companies WHERE id=505").fetchone()[:] == ("wonderboo", "WONDR")
    assert addcompany.add(connection, "sustainion-group", "Sustainion Group", isin="SE0002067421", symbol="SUSG", market="Spotlight") == (6, False)
    assert connection.execute("SELECT isin, symbol, market FROM companies WHERE id=6").fetchone()[:] == ("SE0002067421", "SUSG", "Spotlight")
