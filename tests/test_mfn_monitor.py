from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from capital_radar import mfn_monitor
from capital_radar.config import Settings
from capital_radar.db import Database
from capital_radar.mfn_monitor import MfnMonitor

NOW = "2026-11-01T09:00:00Z"


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "screener.db")
    db.initialize()
    with sqlite3.connect(db.path) as connection:
        for issuer_id, name, active in ((1, "Bevakad AB", 1), (2, "Inaktiv AB", 0)):
            connection.execute(
                "INSERT INTO issuers(id, canonical_name, normalized_name, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)", (issuer_id, name, name.lower(), NOW, NOW))
            connection.execute(
                "INSERT INTO securities(issuer_id, source_id, external_id, isin, symbol, display_name,"
                " market, segment, first_seen_at, last_seen_at, metadata_json)"
                " VALUES (?, 'nasdaq', ?, ?, ?, ?, 'Nasdaq Stockholm', 'Small Cap', ?, ?, '{}')",
                (issuer_id, f"x{issuer_id}", f"SE000000000{issuer_id}", f"S{issuer_id}", name, NOW, NOW))
            connection.execute(
                "INSERT INTO issuer_watchlist(issuer_id, active, added_at, updated_at) VALUES (?, ?, ?, ?)",
                (issuer_id, active, NOW, NOW))
    return db


def test_watched_issuers_only_active(database: Database) -> None:
    monitor = MfnMonitor(database, Settings.from_env(), actions=SimpleNamespace())
    assert monitor.watched_issuers() == [{"id": 1, "canonical_name": "Bevakad AB"}]


def test_run_once_upserts_documents_and_counts_failures(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    upserts = []
    monkeypatch.setattr(database, "upsert_document", lambda candidate, universe_only: upserts.append(candidate) or ("created", True))
    item = SimpleNamespace(
        source_id="mfn", external_id="e1", title="Bevakad AB: emission", canonical_url="https://mfn.se/a",
        published_at=NOW, language="sv", category="regulatory", attachments=[], metadata={}, issuer_name="Bevakad AB",
    )

    class FakeAdapter:
        def __init__(self, http) -> None:
            pass

        def fetch_issuer(self, url, *, issuer_name, issuer_slug, limit):
            return SimpleNamespace(items=[item])

    monkeypatch.setattr(mfn_monitor, "MfnDiscoveryAdapter", FakeAdapter)
    actions = SimpleNamespace(_resolve_mfn_identity=lambda issuer_id, name: ("bevakad", "https://mfn.se/all/a/bevakad"))
    monitor = MfnMonitor(database, Settings.from_env(), actions)
    summary = monitor.run_once()
    assert (summary["issuers"], summary["seen"], summary["created"], summary["failed"]) == (1, 1, 1, 0)
    assert upserts[0].metadata["collection_policy"] == "news_watchlist_only"
    assert monitor.status()["last_run"] == summary

    def broken(issuer_id, name):
        raise RuntimeError("nätverk")

    monitor.actions = SimpleNamespace(_resolve_mfn_identity=broken)
    assert monitor.run_once()["failed"] == 1


def test_collection_is_off_by_default(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from capital_radar.app import create_app

    settings = replace(Settings.from_env(), db_path=tmp_path / "screener.db", documents_path=tmp_path / "docs")
    assert settings.collection_enabled is False
    with TestClient(create_app(settings)) as client:
        assert client.app.state.web.mfn_monitor is None
