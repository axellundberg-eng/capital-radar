from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from capital_radar.db import Database, schema_sql


def _tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            name for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def _columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_gets_screener_tables_and_sources(tmp_path: Path) -> None:
    db = Database(tmp_path / "screener.db")
    db.initialize()
    tables = _tables(db.path)
    assert {"issuers", "documents", "financing_events", "runway_assessments"} <= tables
    # Garantier, CRM och nyhetskommentarer ingår inte.
    assert not {"news_comments", "own_guarantee_offers", "issuer_notes"} & tables
    with db.connect() as connection:
        sources = {row["id"] for row in connection.execute("SELECT id FROM sources")}
    assert {"mfn", "nasdaq_nordic", "ngm"} <= sources


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "screener.db")
    db.initialize()
    with closing(sqlite3.connect(db.path)) as connection:
        before = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    db.initialize()
    with closing(sqlite3.connect(db.path)) as connection:
        after = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert before == after


def test_existing_tables_outside_schema_are_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "screener.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE news_comments (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO news_comments (body) VALUES ('behålls')")
        connection.commit()
    Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT body FROM news_comments").fetchone() == ("behålls",)


def test_missing_nullable_column_is_added_to_older_database(tmp_path: Path) -> None:
    path = tmp_path / "screener.db"
    Database(path).initialize()
    columns = _columns(path, "issuer_analysis_summary")
    # Bygg om tabellen utan en kolumn, som i en äldre databas.
    dropped = next(
        name for name in sorted(columns)
        if name not in {"issuer_id", "analysis_status", "capital_need_decision", "updated_at"}
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"ALTER TABLE issuer_analysis_summary DROP COLUMN {dropped}")
        connection.commit()
    assert dropped not in _columns(path, "issuer_analysis_summary")
    Database(path).initialize()
    assert dropped in _columns(path, "issuer_analysis_summary")


def test_missing_required_column_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "screener.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sources (id TEXT PRIMARY KEY)")
        connection.commit()
    with pytest.raises(RuntimeError, match="sources"):
        Database(path).initialize()


def test_schema_has_no_triggers() -> None:
    # Läsmodellernas triggers från den gamla tjänsten följer inte med.
    assert "CREATE TRIGGER" not in schema_sql().upper()
