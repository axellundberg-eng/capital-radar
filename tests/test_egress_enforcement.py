from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import AgentRunner, reset_pools
from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.ai.router import ExecutionProfile, RetryPolicy
from capital_radar.db import Database


def _profile(classification: str) -> ExecutionProfile:
    return ExecutionProfile(
        provider="deepseek",
        model="deepseek-v4-pro",
        thinking_enabled=True,
        reasoning_effort="high",
        max_output_tokens=4096,
        context_limit=128000,
        connect_timeout=10.0,
        read_timeout=120.0,
        hard_timeout=180.0,
        retry_policy=RetryPolicy(max_attempts=1, backoff_base_seconds=0.0),
        concurrency_pool="analysis_pro",
        priority="active_deal_analysis",
        data_classification=classification,
    )


def _response() -> ProviderResponse:
    return ProviderResponse(
        provider="deepseek",
        model="deepseek-v4-pro",
        content='{"ok": true, "confidence": 0.9}',
        tool_calls=[],
        finish_reason="stop",
        usage=ProviderUsage(input_tokens=10, cached_input_tokens=0, output_tokens=5),
        latency_ms=10,
        request_id="req-egress",
    )


class _RecordingProvider:
    provider_name = "deepseek-pro"

    def __init__(self) -> None:
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        return _response()

    def health_check(self):
        return True


class EgressEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pools()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "egress.db")
        self.db.initialize()
        from capital_radar.models import SecurityCandidate

        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="EG-1",
                issuer_name="Egress AB",
                display_name="Egress",
                symbol="EG",
                isin="SE0000007777",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _runner(self, egress_enabled: bool) -> AgentRunner:
        settings = SimpleNamespace(
            private_data_external_egress_enabled=egress_enabled
        )
        return AgentRunner(self.db, settings=settings, sleep=lambda _: None)

    def _run(self, runner: AgentRunner, provider, classification: str, *, prompt: str = "känsligt användarinnehåll"):
        return runner.run(
            agent_type="executive_chat",
            provider=provider,
            profile=_profile(classification),
            system_prompt="hemlig systemprompt",
            user_prompt=prompt,
            schema={"type": "object"},
            prompt_version="chat-v1",
            issuer_id=1,
        )

    def _recorded_run(self, run_id: int):
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT status, error_type, safe_error_message FROM agent_runs WHERE id=?",
                (run_id,),
            ).fetchone()

    def test_public_classification_calls_provider(self) -> None:
        provider = _RecordingProvider()
        outcome = self._run(self._runner(False), provider, "public")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(provider.calls, 1)

    def test_non_public_blocked_when_egress_disabled(self) -> None:
        for classification in ("private", "potential_mnpi", "confirmed_mnpi"):
            with self.subTest(classification=classification):
                reset_pools()
                provider = _RecordingProvider()
                with self.assertLogs("bergs.ai.agent_runner", level="WARNING") as logs:
                    outcome = self._run(self._runner(False), provider, classification)
                self.assertEqual(outcome.status, "blocked")
                self.assertEqual(outcome.error_type, "egress_blocked")
                self.assertIsNone(outcome.artifact_id)
                self.assertEqual(provider.calls, 0)
                run = self._recorded_run(outcome.run_id)
                # Schema v7: DB persisterar "blocked" (policy-denied före
                # provider-anrop); error_type="egress_blocked" skiljer
                # egress-spärren från circuit-open ("unavailable").
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["error_type"], "egress_blocked")
                # Inget innehåll får loggas eller sparas i felmeddelandet.
                self.assertNotIn("känsligt", run["safe_error_message"])
                self.assertNotIn("hemlig", run["safe_error_message"])
                self.assertIn(classification, run["safe_error_message"])
                for record in logs.records:
                    self.assertNotIn("känsligt användarinnehåll", record.getMessage())
                    self.assertNotIn("hemlig systemprompt", record.getMessage())

    def test_blocked_run_never_stores_artifact(self) -> None:
        provider = _RecordingProvider()
        outcome = self._run(self._runner(False), provider, "confirmed_mnpi")
        self.assertEqual(outcome.status, "blocked")
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_non_public_allowed_when_egress_enabled(self) -> None:
        for classification in ("private", "potential_mnpi", "confirmed_mnpi"):
            with self.subTest(classification=classification):
                reset_pools()
                provider = _RecordingProvider()
                # Unik prompt per klassifikation för att undvika idempotency-träff.
                outcome = self._run(
                    self._runner(True),
                    provider,
                    classification,
                    prompt=f"innehåll för {classification}",
                )
                self.assertEqual(outcome.status, "completed")
                self.assertEqual(provider.calls, 1)

    def test_missing_settings_blocks_non_public(self) -> None:
        # Runner utan settings ska fail-safe blockera icke-publik data.
        runner = AgentRunner(self.db, sleep=lambda _: None)
        provider = _RecordingProvider()
        outcome = self._run(runner, provider, "private")
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
