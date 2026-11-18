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
