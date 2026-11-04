from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.research_channel import DDGSResearchProvider, ResearchChannel, ResearchChannelError


def settings(**overrides):
    values = {
        "research_enabled": True, "research_ddgs_primary_backend": "google",
        "research_ddgs_fallback_backends": ("duckduckgo", "bing"),
        "research_region": "se-sv", "research_safesearch": "moderate",
        "research_timeout_seconds": 8, "research_max_results": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDDGS:
    def __init__(self, calls, *, timeout): self.calls, self.timeout = calls, timeout
    def text(self, query, **kwargs):
        self.calls.append((query, kwargs, self.timeout))
        outcome = kwargs["backend"]
        if outcome == "google":
            return [{"title": "SciBase emissionsvillkor", "href": "https://example.test/a", "body": "Källreferat"}, {"title": "Dubblett", "href": "https://example.test/a"}, {"title": "Fel", "href": "javascript:bad"}]
        return [{"title": "Reservkälla", "href": "https://example.test/fallback", "body": "Sekundär träff"}]


class ResearchChannelTests(unittest.TestCase):
    def test_disabled_status_makes_no_request(self):
        factory = MagicMock()
        channel = ResearchChannel(settings(research_enabled=False), ddgs_factory=factory)
        self.assertEqual(channel.status()["provider"], "disabled")
        with self.assertRaises(ResearchChannelError): channel.search("SciBase emission")
        factory.assert_not_called()

    def test_google_first_uses_swedish_bounded_settings_and_safe_citations(self):
        calls = []
        channel = ResearchChannel(settings(), ddgs_factory=lambda **kwargs: FakeDDGS(calls, **kwargs))
        result = channel.search("SciBase emission", limit=9)
        self.assertEqual(result["provider"], "ddgs")
        self.assertEqual(result["backend"], "google")
        self.assertEqual(result["results"], [{"title": "SciBase emissionsvillkor", "url": "https://example.test/a", "snippet": "Källreferat", "display_url": "example.test"}])
        query, kwargs, timeout = calls[0]
        self.assertEqual(query, "SciBase emission")
        self.assertEqual(kwargs, {"region": "se-sv", "safesearch": "moderate", "max_results": 5, "backend": "google"})
        self.assertEqual(timeout, 8.0)
        self.assertEqual(len(calls), 1)

    def test_fallback_is_only_used_when_google_errors(self):
        calls = []
        class FailingGoogle(FakeDDGS):
            def text(self, query, **kwargs):
                self.calls.append((query, kwargs, self.timeout))
                if kwargs["backend"] == "google": raise RuntimeError("provider failure")
                return [{"title": "Reservkälla", "href": "https://example.test/fallback", "body": "Sekundär träff"}]
        provider = DDGSResearchProvider(timeout_seconds=8, primary_backend="google", fallback_backends=("duckduckgo", "bing"), region="se-sv", safesearch="moderate", ddgs_factory=lambda **kwargs: FailingGoogle(calls, **kwargs))
        backend, results = provider.search("Teneo AI", limit=2)
        self.assertEqual(backend, "duckduckgo")
        self.assertEqual([entry[1]["backend"] for entry in calls], ["google", "duckduckgo"])
        self.assertEqual(results[0].url, "https://example.test/fallback")

    def test_invalid_configuration_and_query_never_create_network_client(self):
        factory = MagicMock()
        channel = ResearchChannel(settings(), ddgs_factory=factory)
        for query in ("x", "x" * 513):
            with self.subTest(query=len(query)), self.assertRaises(ResearchChannelError): channel.search(query)
        factory.assert_not_called()
        with self.assertRaises(ValueError): DDGSResearchProvider(timeout_seconds=8, primary_backend="brave", fallback_backends=(), region="se-sv", safesearch="moderate")
        with self.assertRaises(ValueError): DDGSResearchProvider(timeout_seconds=8, primary_backend="google", fallback_backends=(), region="us-en", safesearch="moderate")


if __name__ == "__main__":
    unittest.main()
