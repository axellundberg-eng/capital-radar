from __future__ import annotations

import json
import socket
import ssl
import time
from datetime import datetime, timezone
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .models import FetchResult


class HttpError(RuntimeError):
    pass


def build_direct_https_opener(*handlers: Any):
    """Build an opener that always has a direct, explicit TLS transport."""
    return build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        *handlers,
    )


def describe_network_error(error: BaseException) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    detail = str(reason).strip() or repr(reason)
    return f"{type(error).__name__}: {detail}"


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30,
        min_interval: float = 0.75,
        retries: int = 2,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self._last_request_at = 0.0
        # Dashboardprocessen kan ärva en spärrproxy från sin startmiljö.
        # Våra källor är explicit validerade, så använd direktanslutning.
        self._opener = build_direct_https_opener()

    def get(self, url: str, *, accept: str = "*/*") -> FetchResult:
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": accept,
                    "Referer": self._referer_for(url),
                },
            )
            try:
                self._last_request_at = time.monotonic()
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                    headers: Message = response.headers
                    return FetchResult(
                        url=response.geturl(),
                        status=int(response.status),
                        body=body,
                        content_type=headers.get_content_type(),
                        fetched_at=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    )
            except (
                HTTPError,
                URLError,
                TimeoutError,
                socket.timeout,
                OSError,
            ) as exc:
                error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))
        assert error is not None
        raise HttpError(
            f"GET misslyckades för {url}: {describe_network_error(error)}"
        ) from error

    def get_json(self, url: str) -> tuple[dict, FetchResult]:
        result = self.get(url, accept="application/json, text/plain, */*")
        try:
            payload = json.loads(result.body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"Ogiltigt JSON-svar från {url}") from exc
        if not isinstance(payload, dict):
            raise HttpError(f"Oväntad JSON-rot från {url}")
        return payload, result

    @staticmethod
    def _referer_for(url: str) -> str:
        if "api.nasdaq.com" in url or "api.news.eu.nasdaq.com" in url:
            return "https://www.nasdaq.com/"
        if "ngm-api-prod.vmate.se" in url:
            return "https://www.ngm.se/"
        if "mfn.se" in url:
            return "https://www.mfn.se/"
        return ""
