from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capital_radar.app import create_app
from capital_radar.config import Settings
from capital_radar.db import Database

NOW = "2026-11-01T09:00:00Z"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    settings = replace(
        Settings.from_env(),
        db_path=tmp_path / "screener.db",
        documents_path=tmp_path / "documents",
        deepseek_api_key=None,
    )
    Database(settings.db_path).initialize()
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute(
            "INSERT INTO issuers(id, canonical_name, normalized_name, created_at, updated_at)"
            " VALUES (1, 'Exempel AB', 'exempel ab', ?, ?)",
            (NOW, NOW),
        )
    return settings


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, start_workers=False))


def test_index_and_assets_are_served_with_csp(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "Capital Radar" in index.text
    assert index.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in index.headers["content-security-policy"]
    for path in ("/workspace.js", "/admin-auth.js", "/styles.css", "/inter-400.ttf"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["x-content-type-options"] == "nosniff"


def test_removed_features_are_gone(client: TestClient) -> None:
    index = client.get("/").text
    assert "guarantees.js" not in index
    assert 'data-route="crm"' not in index
    assert client.get("/guarantees.js").status_code == 404
    assert client.get("/api/crm").status_code == 404


def test_issuer_detail_and_validation(client: TestClient) -> None:
    detail = client.get("/api/issuer/1")
    assert detail.status_code == 200
    body = detail.json()
    assert body["issuer"]["canonical_name"] == "Exempel AB"
    assert "crm" not in body
    assert client.get("/api/issuer/999").status_code == 404
    assert client.get("/api/issuer/abc").status_code == 400


def test_read_endpoints_answer_without_read_model(client: TestClient) -> None:
    for path in (
        "/api/overview",
        "/api/issuer-markets",
        "/api/issuers",
        "/api/radar/analysis?issuer_ids=1",
        "/api/market-health",
        "/api/operations/summary",
        "/api/research/status",
        "/api/research/history",
        "/api/transactions?page=1&page_size=10",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "no-store"


def test_invalid_inputs_keep_old_error_format(client: TestClient) -> None:
    assert client.get("/api/radar/analysis?issuer_ids=0").json() == {"error": "Ogiltigt bolagsurval"}
    page = client.get("/api/transactions?page=1&page_size=50")
    assert page.status_code == 400
    assert page.json()["code"] == "invalid_transaction_page"
    missing = client.get("/api/transactions/transaction:999:rights_issue:1")
    assert missing.status_code in {400, 404}


def test_writes_require_admin_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENER_ADMIN_API_TOKEN", "test-admin-token")
    payload = {"transaction_id": "x", "weight": 0.5, "reason": "test", "confirm": True}
    denied = client.post("/api/radar/weight", json=payload)
    assert denied.status_code == 401
    assert denied.json() == {"error": "Giltig Bearer-token krävs för denna endpoint", "code": "auth_required"}
    wrong = client.post("/api/radar/weight", json=payload, headers={"Authorization": "Bearer fel"})
    assert wrong.status_code == 401
    assert client.post("/api/transactions/x/editorial").status_code == 401

    assert client.get("/api/auth/capabilities").status_code == 401
    identity = client.get("/api/auth/capabilities", headers={"Authorization": "Bearer test-admin-token"})
    assert identity.status_code == 200
    assert identity.json()["role"] == "admin"
    assert "admin" in identity.json()["permissions"]


def test_preview_is_public_and_validated(client: TestClient) -> None:
    assert client.post("/api/radar/preview", json={"issuer_id": 0}).status_code == 400
    response = client.post("/api/radar/preview", json={"issuer_id": 1, "weights": {}})
    assert response.status_code == 200
    assert "items" in response.json()


def test_lifespan_starts_and_stops_workers(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.app.state.web.read_model is not None
        response = client.get("/api/transactions?page=1&page_size=10")
        assert response.status_code in {200, 202}
