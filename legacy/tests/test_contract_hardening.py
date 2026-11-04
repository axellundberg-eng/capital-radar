"""Kontrakthärdning: explicit provider i create_job + persisterad 'blocked'.

Täcker två kontraktsförändringar:

1. ``AnalysisStore.create_job`` kräver explicit ``provider`` (keyword-only,
   inget default) så att nya DeepSeek-jobb aldrig av misstag persisteras
   med Moonshot-proveniens.
2. ``agent_runs.status`` CHECK tillåter persisterad ``'blocked'`` (schema
   v7). ``blocked`` = exekvering nekad av explicit policy före
   provider-anrop; ``unavailable`` = provider/resurs/kapabilitet otillgänglig.
   Migrationen är idempotent och bevarar alla rader verbatim.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.agent_runner import AgentRunner, reset_pools
from capital_radar.ai.router import ExecutionProfile, RetryPolicy
from capital_radar.analysis_store import AnalysisCandidate, AnalysisStore
from capital_radar.db import Database


# Pre-v7-formen av agent_runs: CHECK-constrainten känner inte till 'blocked'.
LEGACY_AGENT_RUNS = """
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    issuer_id INTEGER REFERENCES issuers(id),
    deal_id INTEGER,
    agent_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    thinking_enabled INTEGER NOT NULL DEFAULT 0,
    reasoning_effort TEXT,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'v1',
    execution_profile_version TEXT NOT NULL DEFAULT 'v1',
    execution_profile_json TEXT NOT NULL DEFAULT '{}',
    input_hash TEXT NOT NULL,
    source_data_cutoff TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('queued','running','completed','failed','skipped_idempotent','unavailable')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    confidence REAL,
    artifact_id INTEGER REFERENCES agent_artifacts(id),
    error_type TEXT,
    safe_error_message TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(idempotency_key)
);
CREATE INDEX idx_agent_runs_issuer
    ON agent_runs(issuer_id, agent_type, created_at DESC);
CREATE INDEX idx_agent_runs_status
    ON agent_runs(status, started_at);
CREATE INDEX idx_agent_runs_case
    ON agent_runs(case_id, agent_type);
"""


def _candidate() -> AnalysisCandidate:
    return AnalysisCandidate(
        document_id=1,
        issuer_id=1,
        issuer_name="Legacy AB",
        title="Legacy report",
        published_at="2026-01-01",
        profile_category="report",
        language=None,
        content_id=1,
        source_url="https://example.com/r",
        extracted_text="some text",
        text_sha256="abc",
        page_count=1,
    )


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


class _RecordingProvider:
    """Provider som aldrig får anropas vid egress-block."""

    provider_name = "deepseek-pro"

    def __init__(self) -> None:
        self.calls = 0

    def structured_completion(self, **kwargs):
        self.calls += 1
        raise AssertionError("provider får aldrig anropas vid egress-block")

    def health_check(self):
        return True


class ContractHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pools()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "contract.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialized_store(self) -> AnalysisStore:
        db = Database(self.path)
        db.initialize()
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO issuers(
                    canonical_name, normalized_name, created_at, updated_at
                ) VALUES ('Legacy AB', 'legacy ab', '2026-01-01', '2026-01-01')
                """
            )
            connection.execute(
                """
                INSERT INTO documents(
                    issuer_id, event_key, canonical_url, title, published_at,
                    relevance_reason, created_at, updated_at
                ) VALUES (
                    1, 'evt-1', 'https://example.com/r', 'Legacy report',
                    '2026-01-01', 'fixture', '2026-01-01', '2026-01-01'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO document_contents(
                    document_id, source_url, char_count, status,
                    created_at, updated_at
                ) VALUES (
                    1, 'https://example.com/r', 10, 'extracted',
                    '2026-01-01', '2026-01-01'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        return AnalysisStore(db)

    def _create_job_kwargs(self) -> dict:
        return dict(
            candidate=_candidate(),
            model="kimi-k3",
            prompt_version="capital-need-v1",
            input_sha256="input-1",
            selected_char_count=10,
            estimated_input_tokens=5,
            deadline_at="2099-01-01T00:00:00+00:00",
        )

    # ---------------------------------------------------------- create_job

    def test_create_job_signature_requires_provider(self) -> None:
        signature = inspect.signature(AnalysisStore.create_job)
        parameter = signature.parameters["provider"]
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_create_job_without_provider_raises_type_error(self) -> None:
        store = self._initialized_store()
        with self.assertRaises(TypeError):
            store.create_job(**self._create_job_kwargs())

    def test_create_job_moonshot_legacy_path_persists(self) -> None:
        store = self._initialized_store()
        job_id, status, lease = store.create_job(
            provider="moonshot", **self._create_job_kwargs()
        )
        self.assertEqual(status, "created")
        self.assertIsNotNone(lease)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT provider FROM extraction_jobs WHERE id=?", (job_id,)
            ).fetchone()
            self.assertEqual(row["provider"], "moonshot")
        finally:
            connection.close()

    def test_create_job_deepseek_path_persists(self) -> None:
        store = self._initialized_store()
        kwargs = self._create_job_kwargs()
        kwargs["model"] = "deepseek-chat"
        kwargs["prompt_version"] = "capital-need-v2"
        job_id, status, _ = store.create_job(provider="deepseek", **kwargs)
        self.assertEqual(status, "created")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT provider, model FROM extraction_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            self.assertEqual(row["provider"], "deepseek")
            self.assertEqual(row["model"], "deepseek-chat")
        finally:
            connection.close()

    # --------------------------------------- om-extraktion av tomma jobb

    def test_completed_job_with_zero_facts_is_reclaimed(self) -> None:
        # Ett jobb som markerats 'completed' men gav 0 financial_facts OCH 0
        # financing_events (t.ex. kört på NULL-text vid MFN-fel) ska inte
        # låsas av idempotency — det ska återtas och köras om.
        store = self._initialized_store()
        job_id, status, lease = store.create_job(
            provider="deepseek", **self._create_job_kwargs()
        )
        self.assertEqual(status, "created")
        # Markera jobbet som completed UTAN att skriva några facts/events.
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE extraction_jobs SET status='completed', "
                "lease_token=NULL WHERE id=?",
                (job_id,),
            )
            connection.commit()
        finally:
            connection.close()
        # Samma input ska nu återtas (inte 'completed').
        reclaimed_id, reclaimed_status, new_lease = store.create_job(
            provider="deepseek", **self._create_job_kwargs()
        )
        self.assertEqual(reclaimed_id, job_id)
        self.assertEqual(reclaimed_status, "reclaimed")
        self.assertIsNotNone(new_lease)

    def test_completed_job_with_facts_stays_idempotent(self) -> None:
        # Ett completed-jobb MED minst ett fact ska förbli idempotent
        # (skipped) — om-extraktionen får inte köra om lyckade jobb.
        store = self._initialized_store()
        job_id, status, _ = store.create_job(
            provider="deepseek", **self._create_job_kwargs()
        )
        self.assertEqual(status, "created")
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE extraction_jobs SET status='completed', "
                "lease_token=NULL WHERE id=?",
                (job_id,),
            )
            connection.execute(
                """
                INSERT INTO financial_facts(
                    issuer_id, document_id, extraction_job_id, fact_type,
                    reported_value, normalized_value, currency, reported_unit,
                    period_start, period_end, as_of_date, evidence_label,
                    source_page, source_excerpt, confidence, created_at
                ) VALUES (
                    1, 1, ?, 'cash_and_cash_equivalents', '10', 10.0, 'SEK',
                    'MSEK', NULL, NULL, '2026-06-30', 'label', 1, 'excerpt',
                    0.9, '2026-01-01'
                )
                """,
                (job_id,),
            )
            connection.commit()
        finally:
            connection.close()
        repeated_id, repeated_status, repeated_lease = store.create_job(
            provider="deepseek", **self._create_job_kwargs()
        )
        self.assertEqual(repeated_id, job_id)
        self.assertEqual(repeated_status, "completed")
        self.assertIsNone(repeated_lease)

    # ------------------------------------------------- agent_runs 'blocked'

    def test_agent_runs_accepts_persisted_blocked_status(self) -> None:
        db = Database(self.path)
        db.initialize()
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    agent_type, provider, model, prompt_version, input_hash,
                    status, started_at, idempotency_key, created_at,
                    error_type, safe_error_message
                ) VALUES (
                    'executive_chat', 'deepseek', 'deepseek-v4-pro',
                    'chat-v1', 'hash-1', 'blocked',
                    '2026-01-01T00:00:00+00:00', 'idem-blocked-1',
                    '2026-01-01T00:00:00+00:00', 'egress_blocked',
                    'extern egress blockerad'
                )
                """
            )
            connection.commit()
            row = connection.execute(
                "SELECT status, error_type FROM agent_runs WHERE idempotency_key=?",
                ("idem-blocked-1",),
            ).fetchone()
            self.assertEqual(row["status"], "blocked")
            self.assertEqual(row["error_type"], "egress_blocked")
        finally:
            connection.close()

    def test_egress_block_persists_blocked_without_provider_call(self) -> None:
        db = Database(self.path)
        db.initialize()
        settings = SimpleNamespace(private_data_external_egress_enabled=False)
        runner = AgentRunner(db, settings=settings, sleep=lambda _: None)
        provider = _RecordingProvider()
        with self.assertLogs("bergs.ai.agent_runner", level="WARNING") as logs:
            outcome = runner.run(
                agent_type="executive_chat",
                provider=provider,
                profile=_profile("confirmed_mnpi"),
                system_prompt="hemlig systemprompt",
                user_prompt="känsligt användarinnehåll",
                schema={"type": "object"},
                prompt_version="chat-v1",
                issuer_id=None,
            )
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.error_type, "egress_blocked")
        self.assertEqual(provider.calls, 0)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT status, error_type, safe_error_message
                FROM agent_runs WHERE id=?
                """,
                (outcome.run_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["error_type"], "egress_blocked")
        # Inget privat innehåll får läcka till DB eller loggar.
        self.assertNotIn("känsligt", row["safe_error_message"])
        self.assertNotIn("hemlig", row["safe_error_message"])
        for record in logs.records:
            self.assertNotIn("känsligt användarinnehåll", record.getMessage())
            self.assertNotIn("hemlig systemprompt", record.getMessage())

    # ------------------------------------------------------- migration v7

    def _seed_legacy_agent_runs(self) -> None:
        """Bygg fullt schema, nedgradera sedan agent_runs till pre-v7."""
        db = Database(self.path)
        db.initialize()
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP INDEX IF EXISTS idx_agent_runs_issuer")
            connection.execute("DROP INDEX IF EXISTS idx_agent_runs_status")
            connection.execute("DROP INDEX IF EXISTS idx_agent_runs_case")
            connection.execute("DROP TABLE agent_runs")
            connection.executescript(LEGACY_AGENT_RUNS)
            # Historiska rader som måste överleva migrationen verbatim.
            connection.execute(
                """
                INSERT INTO agent_runs(
                    agent_type, provider, model, prompt_version, input_hash,
                    status, started_at, idempotency_key, created_at,
                    latency_ms, input_tokens, output_tokens, confidence
                ) VALUES (
                    'evidence_validation', 'moonshot', 'kimi-k3',
                    'evidence-audit-v1', 'hash-legacy', 'completed',
                    '2026-01-01T00:00:00+00:00', 'idem-legacy-1',
                    '2026-01-01T00:00:00+00:00', 123, 10, 5, 0.8
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_runs(
                    agent_type, provider, model, prompt_version, input_hash,
                    status, started_at, idempotency_key, created_at,
                    error_type, safe_error_message
                ) VALUES (
                    'executive_chat', 'deepseek', 'deepseek-v4-pro',
                    'chat-v1', 'hash-legacy-2', 'unavailable',
                    '2026-01-02T00:00:00+00:00', 'idem-legacy-2',
                    '2026-01-02T00:00:00+00:00', 'egress_blocked',
                    'extern egress blockerad'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _agent_runs_rows(self) -> list:
        connection = self._connect()
        try:
            return connection.execute(
                """
                SELECT agent_type, provider, model, prompt_version,
                       input_hash, status, started_at, idempotency_key,
                       created_at, latency_ms, input_tokens, output_tokens,
                       confidence, error_type, safe_error_message
                FROM agent_runs ORDER BY id
                """
            ).fetchall()
        finally:
            connection.close()

    def test_migration_v7_adds_blocked_and_preserves_rows(self) -> None:
        self._seed_legacy_agent_runs()
        before = [dict(row) for row in self._agent_runs_rows()]
        self.assertEqual(len(before), 2)

        db = Database(self.path)
        db.initialize()  # kör migrationen mot pre-v7-tabellen

        connection = self._connect()
        try:
            schema_row = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name='agent_runs'
                """
            ).fetchone()
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIn("'blocked'", str(schema_row["sql"]))
        self.assertGreaterEqual(version, 7)

        after = [dict(row) for row in self._agent_runs_rows()]
        self.assertEqual(after, before, "alla rader ska bevaras verbatim")

        # Efter migrationen accepteras persisterad 'blocked'.
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    agent_type, provider, model, prompt_version, input_hash,
                    status, started_at, idempotency_key, created_at
                ) VALUES (
                    'executive_chat', 'deepseek', 'deepseek-v4-pro',
                    'chat-v1', 'hash-post', 'blocked',
                    '2026-01-03T00:00:00+00:00', 'idem-post-1',
                    '2026-01-03T00:00:00+00:00'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_migration_v7_is_idempotent(self) -> None:
        self._seed_legacy_agent_runs()
        db = Database(self.path)
        db.initialize()
        once = [dict(row) for row in self._agent_runs_rows()]

        # Andra initialize() ska returnera tidigt utan att röra tabellen.
        db_again = Database(self.path)
        db_again.initialize()
        twice = [dict(row) for row in self._agent_runs_rows()]
        self.assertEqual(twice, once)

        connection = self._connect()
        try:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM schema_migrations
                WHERE version=7 AND name='agent_runs_blocked_status'
                """
            ).fetchone()[0]
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)
        self.assertGreaterEqual(version, 7)


if __name__ == "__main__":
    unittest.main()
