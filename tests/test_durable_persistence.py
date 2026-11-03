"""Regressionstest för durable persistence över omstart.

Simulerar en Railway-service-restart genom att öppna en helt ny
``Database``-instans (och därmed en ny SQLite-anslutning) mot samma
databasfil på disk. Verifierar att:

- agent_artifacts och agent_runs överlever "omstarten"
- schema version bevaras
- samma idempotenta input återanvänder befintligt artifact efter omstart
  istället för att anropa providern på nytt

Testet är helt lokalt och kräver ingen Railway-miljö.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import AgentRunner
from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.ai.router import ModelRouter
from capital_radar.db import Database


class _CountingProvider:
    """Provider som räknar anrop — används för att bevisa idempotency."""

    def __init__(self) -> None:
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        return ProviderResponse(
            provider="deepseek",
            model="deepseek-v4-flash",
            content='{"verdict": "supported", "per_fact": [], "confidence": 0.8}',
            finish_reason="stop",
            usage=ProviderUsage(input_tokens=10, output_tokens=5),
        )


def _make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        deepseek_flash_model="deepseek-v4-flash",
        deepseek_flash_thinking=False,
        deepseek_flash_max_output_tokens=4096,
        kimi_model="kimi-k3",
        kimi_max_output_tokens=8000,
        kimi_max_input_chars=120000,
    )


class DurablePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "durable.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _runner_kwargs(self, profile) -> dict:
        return dict(
            agent_type="evidence_validation",
            artifact_kind="evidence_audit",
            issuer_id=None,
            profile=profile,
            prompt_version="evidence-audit-v1",
            system_prompt="system",
            user_prompt="durable persistence probe",
            schema={"type": "object"},
        )

    def test_artifacts_and_runs_survive_reopen(self) -> None:
        settings = _make_settings()
        profile = ModelRouter(settings).route("evidence_validation").profile

        # --- "Före restart": skapa artifact + agent_run ---
        db_before = Database(self.db_path)
        db_before.initialize()
        provider = _CountingProvider()
        runner_before = AgentRunner(db_before)
        first = runner_before.run(
            provider=provider, **self._runner_kwargs(profile)
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(provider.calls, 1)

        with db_before.connect() as connection:
            artifacts_before = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
            runs_before = connection.execute(
                "SELECT COUNT(*) FROM agent_runs"
            ).fetchone()[0]
            version_before = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        self.assertEqual(artifacts_before, 1)
        self.assertGreaterEqual(runs_before, 1)
        self.assertGreaterEqual(version_before, 7)

        # --- "Restart": helt ny Database-instans mot samma fil ---
        db_after = Database(self.db_path)
        db_after.initialize()  # idempotent — ska inte förstöra data

        with db_after.connect() as connection:
            artifacts_after = connection.execute(
                "SELECT COUNT(*) FROM agent_artifacts"
            ).fetchone()[0]
            runs_after = connection.execute(
                "SELECT COUNT(*) FROM agent_runs"
            ).fetchone()[0]
            version_after = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            artifact_row = connection.execute(
                "SELECT id, content_json FROM agent_artifacts ORDER BY id LIMIT 1"
            ).fetchone()

        self.assertEqual(
            artifacts_after,
            artifacts_before,
            "agent_artifacts ska överleva omstart",
        )
        self.assertEqual(
            runs_after,
            runs_before,
            "agent_runs ska överleva omstart",
        )
        self.assertEqual(version_after, version_before)
        self.assertIsNotNone(artifact_row)
        self.assertEqual(
            artifact_row["id"],
            first.artifact_id,
            "samma artifact-ID ska finnas kvar efter omstart",
        )

        # --- Idempotency efter restart: samma input återanvänder artifact ---
        provider_after = _CountingProvider()
        runner_after = AgentRunner(db_after)
        second = runner_after.run(
            provider=provider_after, **self._runner_kwargs(profile)
        )
        self.assertEqual(
            second.status,
            "skipped_idempotent",
            "samma input efter omstart ska återanvända befintligt artifact",
        )
        self.assertEqual(
            provider_after.calls,
            0,
            "provider får inte anropas på nytt för samma input hash",
        )
        self.assertEqual(second.artifact_id, first.artifact_id)


if __name__ == "__main__":
    unittest.main()
