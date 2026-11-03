"""Bounded DDGS research for public market intelligence.

The channel only returns citations. It never downloads result pages, persists a
query/result, or promotes a search snippet to financial evidence. DDGS is used
server-side; the browser does not learn its provider configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit


class ResearchChannelError(RuntimeError):
    """A safe research-channel error suitable for API clients."""


@dataclass(frozen=True)
class ResearchResult:
    title: str
    url: str
    snippet: str
    display_url: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "display_url": self.display_url}


class DDGSResearchProvider:
    """Google-first DDGS adapter with a bounded free fallback chain.

    DDGS's Google backend is a best-effort metasearch provider, not Google's
    official API. A returned primary result stops the fallback chain;
    DuckDuckGo and Bing are only used for empty/error responses.
    """

    def __init__(
        self, *, timeout_seconds: float, primary_backend: str,
        fallback_backends: Iterable[str], region: str, safesearch: str,
        ddgs_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout_seconds = min(20.0, max(1.0, float(timeout_seconds)))
        self._backends = _backend_chain(primary_backend, fallback_backends)
        self._region = _region(region)
        self._safesearch = _safesearch(safesearch)
        self._factory = ddgs_factory or _load_ddgs

    def search(self, query: str, *, limit: int) -> tuple[str, list[ResearchResult]]:
        query = _query(query)
        limit = max(1, min(int(limit), 10))
        for backend in self._backends:
            try:
                rows = self._factory(timeout=self._timeout_seconds).text(
                    query, region=self._region, safesearch=self._safesearch,
                    max_results=limit, backend=backend,
                )
                results = _results(rows, limit=limit)
            except Exception:  # DDGS exposes version-specific provider exceptions.
                continue
            if results:
                return backend, results
        raise ResearchChannelError("Extern sökning är tillfälligt otillgänglig")


class ResearchChannel:
    """Provider-neutral external research facade used by the HTTP API."""

    def __init__(self, settings: Any, *, ddgs_factory: Callable[..., Any] | None = None) -> None:
        self._max_results = max(1, min(10, int(getattr(settings, "research_max_results", 5))))
        self._provider = (
            DDGSResearchProvider(
                timeout_seconds=float(getattr(settings, "research_timeout_seconds", 8.0)),
                primary_backend=str(getattr(settings, "research_ddgs_primary_backend", "google")),
                fallback_backends=getattr(settings, "research_ddgs_fallback_backends", ("duckduckgo", "bing")),
                region=str(getattr(settings, "research_region", "se-sv")),
                safesearch=str(getattr(settings, "research_safesearch", "moderate")),
                ddgs_factory=ddgs_factory,
            ) if bool(getattr(settings, "research_enabled", False)) else None
        )

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "provider": "ddgs" if self.enabled else "disabled",
            "primary_backend": self._provider._backends[0] if self._provider else None,
            "fallback_backends": list(self._provider._backends[1:]) if self._provider else [],
            "region": self._provider._region if self._provider else None,
            "max_results": self._max_results, "search_requires_bearer": True,
            "writes_require_bearer": True,
            "persistence": "validated_history",
            "validation_posture": "trusted_primary_sources_auto_approved",
        }

    def search(self, query: str, *, limit: int | None = None) -> dict[str, Any]:
        if self._provider is None:
            raise ResearchChannelError("Extern research är inte konfigurerad")
        maximum = self._max_results if limit is None else min(self._max_results, int(limit))
        backend, results = self._provider.search(query, limit=maximum)
        return {
            "query": _query(query), "provider": "ddgs", "backend": backend,
            "results": [result.as_dict() for result in results],
            "evidence_note": "Sökresultat är navigationskällor och inte verifierade analysfakta.",
        }


def _load_ddgs(*, timeout: float) -> Any:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise ResearchChannelError("DDGS är inte installerat i körmiljön") from exc
    return DDGS(timeout=timeout)


def _backend_chain(primary: str, fallbacks: Iterable[str]) -> tuple[str, ...]:
    allowed = {"google", "duckduckgo", "bing"}
    values = [str(primary).strip().casefold(), *(str(item).strip().casefold() for item in fallbacks)]
    chain: list[str] = []
    for value in values:
        if value not in allowed:
            raise ValueError("DDGS-backend måste vara google, duckduckgo eller bing")
        if value not in chain:
            chain.append(value)
    if not chain:
        raise ValueError("Minst en DDGS-backend krävs")
    return tuple(chain)


def _region(value: str) -> str:
    region = value.strip().casefold()
    if region != "se-sv":
        raise ValueError("DDGS-region måste vara se-sv för Capital Radar")
    return region


def _safesearch(value: str) -> str:
    setting = value.strip().casefold()
    if setting not in {"on", "moderate", "off"}:
        raise ValueError("DDGS safesearch måste vara on, moderate eller off")
    return setting


def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise ResearchChannelError("Sökfrågan måste vara text")
    query = " ".join(value.split())
    if not 2 <= len(query) <= 512:
        raise ResearchChannelError("Sökfrågan måste vara 2–512 tecken")
    return query


def _results(rows: Any, *, limit: int) -> list[ResearchResult]:
    if not isinstance(rows, list):
        return []
    results: list[ResearchResult] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        title = _text(row.get("title"))
        url = _safe_url(_text(row.get("href") or row.get("url")))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append(ResearchResult(
            title=title[:300], url=url,
            snippet=_text(row.get("body") or row.get("snippet") or row.get("description"))[:1_000],
            display_url=urlsplit(url).netloc[:200],
        ))
        if len(results) == limit:
            break
    return results


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return value
