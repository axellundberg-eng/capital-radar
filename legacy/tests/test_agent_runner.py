from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import (
    AgentRunner,
    compute_idempotency_key,
    compute_input_hash,
    reset_pools,
)
from capital_radar.ai.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.ai.router import ExecutionProfile, RetryPolicy
from capital_radar.db import Database


def _profile(**overrides) -> ExecutionProfile:
    base = dict(
        provider="deepseek",
        model="deepseek-v4-flash",
        thinking_enabled=False,
        reasoning_effort=None,
        max_output_tokens=4096,
        context_limit=128000,
        connect_timeout=10.0,
        read_timeout=120.0,
        hard_timeout=180.0,
        retry_policy=RetryPolicy(max_attempts=2, backoff_base_seconds=0.0),
        concurrency_pool="background_flash",
        priority="background_refresh",
    )
    base.update(overrides)
    return ExecutionProfile(**base)


def _response(content: str = '{"ok": true, "confidence": 0.8}') -> ProviderResponse:
    return ProviderResponse(
        provider="deepseek",
        model="deepseek-v4-flash",
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage=ProviderUsage(input_tokens=100, cached_input_tokens=20, output_tokens=30),
        latency_ms=50,
        request_id="req-1",
    )


class _FakeProvider:
    provider_name = "deepseek-flash"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def health_check(self):
        return True


class AgentRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pools()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "runs.db")
        self.db.initialize()
        from capital_radar.models import SecurityCandidate

        run_id = self.db.start_run("nasdaq_nordic", "universe", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id="AR-1",
                issuer_name="Agent Runner AB",
                display_name="AgentRunner",
                symbol="AR",
                isin="SE0000009999",
                currency="SEK",
                market="Nasdaq Stockholm",
                segment="Small Cap",
            ),
            run_id,
        )
        self.db.finish_run(run_id, status="success")
        self.runner = AgentRunner(self.db, sleep=lambda _: None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, provider, **overrides):
        kwargs = dict(
            agent_type="evidence_validation",
            provider=provider,
            profile=_profile(),
            system_prompt="s",
            user_prompt="u",
            schema={"type": "object"},
            prompt_version="evidence-audit-v1",
            issuer_id=1,
        )
        kwargs.update(overrides)
        return self.runner.run(**kwargs)

    def test_completed_run_records_usage_and_artifact(self) -> None:
        provider = _FakeProvider([_response()])
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "completed")
        self.assertIsNotNone(outcome.artifact_id)
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT * FROM agent_runs WHERE id=?", (outcome.run_id,)
            ).fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["input_tokens"], 100)
        self.assertEqual(run["cached_input_tokens"], 20)
        self.assertEqual(run["output_tokens"], 30)
        self.assertEqual(run["provider_request_id"], "req-1")
        self.assertEqual(run["prompt_version"], "evidence-audit-v1")
        self.assertEqual(run["provider"], "deepseek")
        self.assertAlmostEqual(run["confidence"], 0.8)
        profile_json = json.loads(run["execution_profile_json"])
        self.assertEqual(profile_json["model"], "deepseek-v4-flash")

    def test_idempotent_rerun_skips_provider_call(self) -> None:
        provider = _FakeProvider([_response()])
        first = self._run(provider)
        second = self._run(provider)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "skipped_idempotent")
        self.assertEqual(second.artifact_id, first.artifact_id)
        self.assertEqual(provider.calls, 1)

    def test_same_content_hash_reuses_artifact(self) -> None:
        provider = _FakeProvider([_response(), _response()])
        first = self._run(provider)
        # Samma innehåll men annan prompt → nytt input hash → nytt run,
        # men artifact ska återanvändas via content hash.
        second = self._run(provider, user_prompt="annan prompt")
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.artifact_id, first.artifact_id)
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_new_content_creates_new_version(self) -> None:
        provider = _FakeProvider(
            [_response('{"v": 1}'), _response('{"v": 2}')]
        )
        first = self._run(provider)
        second = self._run(provider, user_prompt="annan prompt")
        self.assertNotEqual(second.artifact_id, first.artifact_id)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT version_no FROM agent_artifacts ORDER BY version_no"
            ).fetchall()
        self.assertEqual([r["version_no"] for r in rows], [1, 2])

    def test_missing_provider_marks_unavailable(self) -> None:
        outcome = self._run(None, unavailable_reason="DeepSeek-nyckel saknas")
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.error_type, "provider_unavailable")
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT status, safe_error_message FROM agent_runs WHERE id=?",
                (outcome.run_id,),
            ).fetchone()
        self.assertEqual(run["status"], "unavailable")
        self.assertIn("DeepSeek", run["safe_error_message"])

    def test_rate_limit_is_retried_once(self) -> None:
        provider = _FakeProvider(
            [ProviderRateLimitError("429"), _response()]
        )
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(outcome.retry_count, 1)

    def test_schema_error_is_not_retried(self) -> None:
        provider = _FakeProvider([ProviderError("invalid request")])
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(outcome.error_type, "ProviderError")

    def test_invalid_json_output_fails_without_artifact(self) -> None:
        provider = _FakeProvider([_response("inte json alls")])
        outcome = self._run(provider)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_type, "invalid_output")
        self.assertIsNone(outcome.artifact_id)
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_validation_failure_not_saved_as_completed(self) -> None:
        provider = _FakeProvider([_response('{"bad": true}')])
        outcome = self._run(provider, validate=lambda parsed: (False, None, []))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_type, "schema_validation_failed")
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT status FROM agent_runs WHERE id=?", (outcome.run_id,)
            ).fetchone()
        self.assertEqual(run["status"], "failed")

    def test_failed_rerun_does_not_delete_previous_artifact(self) -> None:
        provider = _FakeProvider([_response()])
        first = self._run(provider)
        failing = _FakeProvider([ProviderUnavailableError("nere")])
        second = self._run(failing, user_prompt="ny input")
        self.assertEqual(second.status, "failed")
        with self.db.connect() as connection:
            artifact = connection.execute(
                "SELECT id FROM agent_artifacts WHERE id=?",
                (first.artifact_id,),
            ).fetchone()
        self.assertIsNotNone(artifact)

    def test_cost_estimation_uses_injected_prices(self) -> None:
        runner = AgentRunner(
            self.db,
            cost_per_1k_input={"deepseek/deepseek-v4-flash": 0.001},
            cost_per_1k_output={"deepseek/deepseek-v4-flash": 0.002},
            sleep=lambda _: None,
        )
        provider = _FakeProvider([_response()])
        outcome = runner.run(
            agent_type="evidence_validation",
            provider=provider,
            profile=_profile(),
            system_prompt="s",
            user_prompt="u",
            schema={},
            prompt_version="v1",
        )
        with self.db.connect() as connection:
            run = connection.execute(
                "SELECT estimated_cost_usd FROM agent_runs WHERE id=?",
                (outcome.run_id,),
            ).fetchone()
        # (100-20)/1000*0.001 + 30/1000*0.002 = 0.00008 + 0.00006
        self.assertAlmostEqual(run["estimated_cost_usd"], 0.00014)

    def test_input_hash_stable(self) -> None:
        h1 = compute_input_hash(
            agent_type="a", prompt_version="v1",
            system_prompt="s", user_prompt="u", model="m",
        )
        h2 = compute_input_hash(
            agent_type="a", prompt_version="v1",
            system_prompt="s", user_prompt="u", model="m",
        )
        self.assertEqual(h1, h2)
        key = compute_idempotency_key(
            agent_type="a", issuer_id=1, case_id=None, input_hash=h1
        )
        self.assertEqual(len(key), 64)


class AgentRunsMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "m.db")
            db.initialize()
            db.initialize()  # andra gången ska inte krascha
            # Det frysta schemat bokför inte gamla migreringsversioner.
            with db.connect() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("agent_runs", tables)
            self.assertIn("agent_artifacts", tables)


if __name__ == "__main__":
    unittest.main()
