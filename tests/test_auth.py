"""PIN-inloggning: skyddade sidor, rätt och fel PIN, spärr efter fem försök, utloggning och kakans giltighet."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from radar import auth
from radar.db import connect
from radar.settings import Settings
from radar.web import create_app

pytestmark = pytest.mark.anonymous


@pytest.fixture()
def client(tmp_path):
    settings = replace(Settings.from_env(), db_path=tmp_path / "radar.db", files_path=tmp_path / "f")
    connect(settings.db_path).close()
    return TestClient(create_app(settings), follow_redirects=False)


def test_everything_but_health_and_login_needs_a_session(client):
    assert client.get("/health").status_code == 200
    page = client.get("/login")
    assert page.status_code == 200 and "Ange PIN" in page.text and "4030" not in page.text   # PIN:en står aldrig i sidan
    home = client.get("/")
    assert home.status_code == 303 and home.headers["location"].endswith("/login")
    for path in ("/api/companies", "/api/prospects", "/api/overview", "/api/flow", "/api/guarantee/1", "/static/app.js"):
        response = client.get(path)
        assert response.status_code in (401, 303), path
    assert client.get("/api/companies").json() == {"error": "inloggning krävs"}


def test_right_pin_gives_a_working_session_and_logout_ends_it(client):
    ok = client.post("/login", json={"pin": "4030"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/overview").status_code == 200
    assert client.get("/login").status_code == 303                          # redan inloggad: vidare till appen
    client.post("/logout")
    assert client.get("/api/overview").status_code == 401


def test_wrong_pin_is_refused_and_five_failures_lock_the_address(client):
    for _ in range(4):
        assert client.post("/login", json={"pin": "1234"}).status_code == 401
    assert client.post("/login", json={"pin": "1234"}).status_code == 429       # femte felet låser
    locked = client.post("/login", json={"pin": "4030"})                        # även rätt PIN nekas under låsningen
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert client.get("/api/overview").status_code == 401
    assert client.post("/login", data="inte json", headers={"Content-Type": "text/plain"}).status_code == 429


def test_malformed_login_counts_as_a_failed_attempt(client):
    assert client.post("/login", data="inte json").status_code == 401
    assert client.post("/login", json={}).status_code == 401


def test_token_expires_and_changes_when_the_pin_changes(monkeypatch):
    key = b"k"
    token = auth.make_token(key, now=1_000_000)
    assert auth.valid_token(key, token, now=1_000_000 + 60)
    assert not auth.valid_token(key, token, now=1_000_000 + auth.SESSION_SECONDS + 1)
    assert not auth.valid_token(b"annan nyckel", token, now=1_000_060)
    assert not auth.valid_token(key, "skräp") and not auth.valid_token(key, None)
    monkeypatch.setenv("RADAR_PIN", "9999")
    assert not auth.valid_token(key, token, now=1_000_060)                      # nytt PIN loggar ut alla


def test_throttle_forgets_old_failures_and_unlocks():
    throttle = auth.Throttle()
    for _ in range(4):
        assert throttle.fail("a", now=100) == 0
    assert throttle.fail("a", now=100) == auth.LOCK_SECONDS and throttle.wait_seconds("a", now=101) > 0
    assert throttle.wait_seconds("a", now=100 + auth.LOCK_SECONDS + 1) == 0
    assert throttle.fail("b", now=100) == 0 and throttle.fail("b", now=100 + auth.LOCK_SECONDS + 5) == 0
