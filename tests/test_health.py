from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from capital_radar.app import create_app
from capital_radar.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "screener.db",
        documents_path=tmp_path / "documents",
        git_sha="test-sha",
        deepseek_api_key="",
    )


def test_health_is_degraded_without_database(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == {"status": "missing"}


def test_health_ok_with_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE issuers (id INTEGER PRIMARY KEY)")
    client = TestClient(create_app(settings))
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["git_sha"] == "test-sha"
    assert body["database"]["journal_mode"] == "wal"


def test_health_never_writes_to_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute("CREATE TABLE issuers (id INTEGER PRIMARY KEY)")
    before = settings.db_path.read_bytes()
    TestClient(create_app(settings)).get("/health")
    assert settings.db_path.read_bytes() == before


def test_openapi_is_generated(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    spec = client.get("/openapi.json").json()
    assert "/health" in spec["paths"]
