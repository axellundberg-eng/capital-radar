"""Gemensamma testinställningar: webbtesterna är inloggade, utom de som uttryckligen testar inloggningen."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def logged_in_test_clients(monkeypatch, request):
    """Alla TestClient får en giltig sessionskaka, så att API-tester inte stoppas av PIN-skyddet.

    Testerna i test_auth.py har markeringen `anonymous` och får en klient utan kaka."""
    monkeypatch.setenv("RADAR_SECRET", "test-secret")
    monkeypatch.setenv("RADAR_PIN", "4030")
    if request.node.get_closest_marker("anonymous"):
        return
    from starlette.testclient import TestClient

    from radar import auth

    original = TestClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.cookies.set(auth.COOKIE, auth.make_token(b"test-secret"))

    monkeypatch.setattr(TestClient, "__init__", init)


def pytest_configure(config):
    config.addinivalue_line("markers", "anonymous: webbklienten är inte inloggad")
