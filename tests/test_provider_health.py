from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import AgentRunner
from capital_radar.ai.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderServerError,
)
from capital_radar.ai.health import (
    CIRCUIT_CLOSED,
    CIRCUIT_HALF_OPEN,
    CIRCUIT_OPEN,
    ProviderHealthRegistry,
)
from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.ai.router import ModelRouter
from capital_radar.db import Database
from capital_radar.models import SecurityCandidate


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _rate_limit() -> ProviderRateLimitError:
    return ProviderRateLimitError("429 too many requests")


def _server_error() -> ProviderServerError:
    return ProviderServerError("503 unavailable", status_code=503)


class HealthRegistryTests(unittest.TestCase):
    def test_closed_allows_requests(self) -> None:
        registry = ProviderHealthRegistry()
        self.assertTrue(registry.allow_request("deepseek/flash"))
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_CLOSED)

    def test_opens_after_threshold_breaking_failures(self) -> None:
        registry = ProviderHealthRegistry(failure_threshold=3)
        for _ in range(3):
            registry.record_failure("deepseek/flash", _rate_limit())
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_OPEN)
        self.assertFalse(registry.allow_request("deepseek/flash"))

    def test_non_breaking_errors_do_not_open_circuit(self) -> None:
        registry = ProviderHealthRegistry(failure_threshold=2)
        for _ in range(5):
            registry.record_failure(
                "deepseek/flash", ProviderAuthError("401")
            )
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_CLOSED)
        snapshot = registry.snapshot("deepseek/flash")
        self.assertEqual(snapshot["total_failures"], 5)

    def test_cooldown_moves_to_half_open_and_probe_closes(self) -> None:
        clock = FakeClock()
        registry = ProviderHealthRegistry(
            failure_threshold=2, cooldown_seconds=60.0, clock=clock
        )
        registry.record_failure("deepseek/flash", _server_error())
        registry.record_failure("deepseek/flash", _server_error())
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_OPEN)
        self.assertFalse(registry.allow_request("deepseek/flash"))
        clock.advance(61.0)
        self.assertTrue(registry.allow_request("deepseek/flash"))
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_HALF_OPEN)
        registry.record_success("deepseek/flash", latency_ms=120.0)
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_CLOSED)

    def test_failure_in_half_open_reopens(self) -> None:
        clock = FakeClock()
        registry = ProviderHealthRegistry(
            failure_threshold=2, cooldown_seconds=10.0, clock=clock
        )
        registry.record_failure("deepseek/flash", _rate_limit())
        registry.record_failure("deepseek/flash", _rate_limit())
        clock.advance(11.0)
        registry.allow_request("deepseek/flash")
        registry.record_failure("deepseek/flash", _rate_limit())
        self.assertEqual(registry.circuit_state("deepseek/flash"), CIRCUIT_OPEN)

    def test_interactive_priority_always_allowed(self) -> None:
        registry = ProviderHealthRegistry(failure_threshold=1)
        registry.record_failure("deepseek/pro", _server_error())
        self.assertEqual(registry.circuit_state("deepseek/pro"), CIRCUIT_OPEN)
        self.assertTrue(
            registry.allow_request("deepseek/pro", priority="interactive_chat")
        )
        self.assertTrue(registry.allow_request("deepseek/pro", priority="critical"))
        self.assertFalse(registry.allow_request("deepseek/pro", priority="background"))

    def test_snapshot_tracks_rates_and_latency(self) -> None:
        registry = ProviderHealthRegistry()
        registry.record_success("deepseek/flash", latency_ms=100.0)
        registry.record_success("deepseek/flash", latency_ms=200.0)
        registry.record_failure("deepseek/flash", _rate_limit())
        snapshot = registry.snapshot("deepseek/flash")
        self.assertEqual(snapshot["total_requests"], 3)
        self.assertEqual(snapshot["total_successes"], 2)
        self.assertAlmostEqual(snapshot["success_rate"], 2 / 3)
        self.assertEqual(snapshot["rate_limit_count"], 1)
        self.assertAlmostEqual(snapshot["average_latency_ms"], 150.0)


class _FailingProvider:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        raise self._error


class _OkProvider:
    def __init__(self) -> None:
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        return ProviderResponse(
            provider="deepseek",
            model="deepseek-v4-flash",
            content='{"ok": true}',
            finish_reason="stop",
            usage=ProviderUsage(input_tokens=5, output_tokens=2),
        )


class RunnerCircuitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary_directory.name) / "health.db")
        self.db.initialize()
        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="HLTH-1",
                issuer_name="Health AB",
                display_name="Health",
                symbol="HLTH",
                isin="SE0000000401",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        with self.db.connect() as connection:
            self.issuer_id = int(
                connection.execute(
                    "SELECT issuer_id FROM securities WHERE external_id='HLTH-1'"
                ).fetchone()[0]
            )
        settings = SimpleNamespace(
            deepseek_flash_model="deepseek-v4-flash",
            deepseek_flash_thinking=False,
            deepseek_flash_max_output_tokens=4096,
            kimi_model="kimi-k3",
            kimi_max_output_tokens=8000,
            kimi_max_input_chars=120000,
        )
        self.profile = ModelRouter(settings).route("evidence_validation").profile

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run_kwargs(self, provider) -> dict:
        return dict(
            agent_type="evidence_validation",
            artifact_kind="evidence_audit",
            issuer_id=self.issuer_id,
            provider=provider,
            profile=self.profile,
            prompt_version="evidence-audit-v1",
            system_prompt="system",
            user_prompt="input",
            schema={"type": "object"},
        )

    def test_open_circuit_marks_run_unavailable_without_calling_provider(self) -> None:
        registry = ProviderHealthRegistry(failure_threshold=1)
        runner = AgentRunner(self.db, health_registry=registry, sleep=lambda _: None)
        failing = _FailingProvider(_server_error())
        first = runner.run(**self._run_kwargs(failing))
        self.assertEqual(first.status, "failed")
        self.assertEqual(registry.circuit_state("deepseek/deepseek-v4-flash"), CIRCUIT_OPEN)

        # Nytt run med annan input (annars träffar idempotency) blockeras.
        kwargs = self._run_kwargs(_OkProvider())
        kwargs["user_prompt"] = "annan input"
        second = runner.run(**kwargs)
        self.assertEqual(second.status, "unavailable")
        self.assertEqual(second.error_type, "circuit_open")
        self.assertEqual(kwargs["provider"].calls, 0)

    def test_success_is_recorded_in_registry(self) -> None:
        registry = ProviderHealthRegistry()
        runner = AgentRunner(self.db, health_registry=registry)
        outcome = runner.run(**self._run_kwargs(_OkProvider()))
        self.assertEqual(outcome.status, "completed")
        snapshot = registry.snapshot("deepseek/deepseek-v4-flash")
        self.assertEqual(snapshot["total_successes"], 1)
        self.assertEqual(snapshot["circuit_state"], CIRCUIT_CLOSED)


if __name__ == "__main__":
    unittest.main()
