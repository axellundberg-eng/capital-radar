"""Tester för selektiv retry med backoff i SafeDocumentFetcher (P0-1).

Verifierar:
- Transienta fel (timeout, connection reset, 429, 5xx) retryas med backoff.
- Permanenta fel (4xx utom 429, valideringsfel) retryas ALDRIG.
- Efter uttömda försök kastas DocumentError.
- _is_retryable klassificerar rätt.
"""

from __future__ import annotations

import socket
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from capital_radar.documents import DocumentError, SafeDocumentFetcher


def _make_fetcher(**overrides) -> SafeDocumentFetcher:
    kwargs = dict(
        allowed_hosts=("example.com",),
        user_agent="test-agent",
        timeout=5.0,
        max_bytes=10 * 1024 * 1024,
        retries=2,
        retry_backoff=0.0,  # ingen riktig sömn i tester
    )
    kwargs.update(overrides)
    return SafeDocumentFetcher(**kwargs)


def _http_error(code: int) -> HTTPError:
    return HTTPError(
        url="https://example.com/report.pdf",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )


class _FakeResponse:
    """Minimal context-manager som efterliknar urllib-response."""

    def __init__(self, body: bytes = b"%PDF-fake", url: str = "https://example.com/report.pdf"):
        self._body = body
        self._url = url
        self.headers = _FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self._url

    def read(self, n=-1):
        if self._body is None:
            return b""
        chunk, self._body = self._body, None
        return chunk


class _FakeHeaders:
    def get(self, key, default=None):
        return default

    def get_content_type(self):
        return "application/pdf"


class _ScriptedOpener:
    """Opener som kastar en sekvens av fel innan den lyckas."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _patch_opener(monkeypatch, fetcher, outcomes):
    opener = _ScriptedOpener(outcomes)
    monkeypatch.setattr(fetcher, "_opener", opener)
    # Hoppa över DNS-verifiering i tester (ingen nätverksåtkomst).
    monkeypatch.setattr("capital_radar.documents.verify_public_addresses", lambda host: None)
    return opener


# ---------------------------------------------------------------------------
# _is_retryable — klassificering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_http_statuses(code):
    assert SafeDocumentFetcher._is_retryable(_http_error(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
def test_permanent_http_statuses_not_retryable(code):
    assert SafeDocumentFetcher._is_retryable(_http_error(code)) is False


def test_timeout_is_retryable():
    assert SafeDocumentFetcher._is_retryable(TimeoutError("timed out")) is True
    assert SafeDocumentFetcher._is_retryable(socket.timeout("timed out")) is True


def test_connection_error_is_retryable():
    assert SafeDocumentFetcher._is_retryable(ConnectionResetError("reset")) is True


def test_urlerror_with_timeout_reason_is_retryable():
    exc = URLError(TimeoutError("timed out"))
    assert SafeDocumentFetcher._is_retryable(exc) is True


def test_urlerror_with_connection_reset_is_retryable():
    exc = URLError(ConnectionResetError("reset"))
    assert SafeDocumentFetcher._is_retryable(exc) is True


def test_urlerror_with_permanent_reason_not_retryable():
    exc = URLError("unknown reason")
    assert SafeDocumentFetcher._is_retryable(exc) is False


# ---------------------------------------------------------------------------
# get() — retry-beteende
# ---------------------------------------------------------------------------


def test_get_succeeds_after_transient_failure(monkeypatch):
    fetcher = _make_fetcher(retries=2)
    opener = _patch_opener(
        monkeypatch,
        fetcher,
        [TimeoutError("timed out"), _FakeResponse()],
    )
    result = fetcher.get("https://example.com/report.pdf")
    assert result.body == b"%PDF-fake"
    assert opener.calls == 2  # första försöket failade, andra lyckades


def test_get_succeeds_after_retryable_http_error(monkeypatch):
    fetcher = _make_fetcher(retries=2)
    opener = _patch_opener(
        monkeypatch,
        fetcher,
        [_http_error(503), _FakeResponse()],
    )
    result = fetcher.get("https://example.com/report.pdf")
    assert result.body == b"%PDF-fake"
    assert opener.calls == 2


def test_get_does_not_retry_permanent_http_error(monkeypatch):
    fetcher = _make_fetcher(retries=3)
    opener = _patch_opener(monkeypatch, fetcher, [_http_error(404)])
    with pytest.raises(DocumentError):
        fetcher.get("https://example.com/report.pdf")
    assert opener.calls == 1  # aldrig retry på 404


def test_get_exhausts_retries_then_raises(monkeypatch):
    fetcher = _make_fetcher(retries=2)
    opener = _patch_opener(
        monkeypatch,
        fetcher,
        [TimeoutError("t1"), TimeoutError("t2"), TimeoutError("t3")],
    )
    with pytest.raises(DocumentError, match="Dokumenthämtning misslyckades"):
        fetcher.get("https://example.com/report.pdf")
    assert opener.calls == 3  # 1 initial + 2 retries


def test_get_with_zero_retries_makes_single_attempt(monkeypatch):
    fetcher = _make_fetcher(retries=0)
    opener = _patch_opener(monkeypatch, fetcher, [TimeoutError("timed out")])
    with pytest.raises(DocumentError):
        fetcher.get("https://example.com/report.pdf")
    assert opener.calls == 1


def test_get_does_not_retry_validation_error(monkeypatch):
    # Ogiltig URL (inte HTTPS) — permanent valideringsfel, ingen opener involveras.
    fetcher = _make_fetcher(retries=3)
    with pytest.raises(DocumentError, match="HTTPS"):
        fetcher.get("http://example.com/report.pdf")


def test_get_does_not_retry_disallowed_host(monkeypatch):
    fetcher = _make_fetcher(retries=3)
    with pytest.raises(DocumentError, match="inte godkänd"):
        fetcher.get("https://evil.com/report.pdf")
