from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.ai.router import ExecutionProfile, ModelRouter, RetryPolicy
from capital_radar.config import Settings


# Pro-profilens standardmodell styrs av Settings (ändrad i d17d1ab).
PRO_MODEL = Settings.deepseek_pro_model


class ModelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(SimpleNamespace())

    def test_deterministic_tasks_use_no_ai(self) -> None:
        for task in ("universe_filter", "document_relevance"):
            route = self.router.route(task)
            self.assertFalse(route.use_ai, task)
            self.assertEqual(route.profile.provider, "none")
            self.assertEqual(route.profile.concurrency_pool, "background_zero_token")

    def test_financial_extraction_routes_to_flash(self) -> None:
        route = self.router.route("financial_extraction")
        self.assertTrue(route.use_ai)
        self.assertEqual(route.profile.provider, "deepseek")
        self.assertEqual(route.profile.model, "deepseek-v4-flash")
        self.assertEqual(route.profile.concurrency_pool, "background_flash")

    def test_financial_extraction_pro_escalation_route(self) -> None:
        route = self.router.route("financial_extraction_pro")
        self.assertTrue(route.use_ai)
        self.assertEqual(route.profile.provider, "deepseek")
        self.assertEqual(route.profile.model, PRO_MODEL)
        self.assertEqual(route.profile.concurrency_pool, "analysis_pro")

    def test_flash_tasks(self) -> None:
        for task in (
            "evidence_validation",
            "ownership_classification",
            "financing_history_normalization",
        ):
            route = self.router.route(task)
            self.assertEqual(route.profile.provider, "deepseek", task)
            self.assertEqual(route.profile.model, "deepseek-v4-flash", task)
            self.assertFalse(route.profile.thinking_enabled, task)
            self.assertIsNone(route.profile.reasoning_effort, task)
            self.assertEqual(route.profile.concurrency_pool, "background_flash", task)

    def test_pro_tasks(self) -> None:
        for task in (
            "funding_need_analysis",
            "capital_access_analysis",
            "bridge_risk_analysis",
            "subscription_probability",
            "guarantee_underwriting",
            "position_sizing",
            "senior_synthesis",
        ):
            route = self.router.route(task)
            self.assertEqual(route.profile.provider, "deepseek", task)
            self.assertEqual(route.profile.model, PRO_MODEL, task)
            self.assertTrue(route.profile.thinking_enabled, task)
            self.assertEqual(route.profile.reasoning_effort, "high", task)
            self.assertEqual(route.profile.concurrency_pool, "analysis_pro", task)

    def test_pro_max_tasks(self) -> None:
        for task in ("critic", "exceptional_escalation"):
            route = self.router.route(task)
            self.assertEqual(route.profile.provider, "deepseek", task)
            self.assertEqual(route.profile.model, PRO_MODEL, task)
            self.assertTrue(route.profile.thinking_enabled, task)
            self.assertEqual(route.profile.reasoning_effort, "max", task)
            self.assertEqual(route.profile.concurrency_pool, "critical_pro", task)
            self.assertEqual(route.profile.priority, "critical", task)

    def test_pro_max_is_not_an_alias_of_pro_high(self) -> None:
        pro_high = self.router.route("funding_need_analysis").profile
        pro_max = self.router.route("critic").profile
        self.assertNotEqual(pro_high.reasoning_effort, pro_max.reasoning_effort)
        self.assertEqual(pro_max.reasoning_effort, "max")
        self.assertNotEqual(pro_high.concurrency_pool, pro_max.concurrency_pool)

    def test_executive_chat_uses_chat_pool_and_private_classification(self) -> None:
        route = self.router.route("executive_chat")
        self.assertEqual(route.profile.model, PRO_MODEL)
        self.assertEqual(route.profile.concurrency_pool, "interactive_chat")
        self.assertEqual(route.profile.priority, "interactive_chat")
        self.assertEqual(route.profile.data_classification, "private")

    def test_unknown_task_falls_back_to_no_ai(self) -> None:
        route = self.router.route("does_not_exist")
        self.assertFalse(route.use_ai)
        self.assertEqual(route.profile.provider, "none")
        self.assertIn("does_not_exist", route.reason)

    def test_profile_is_immutable(self) -> None:
        route = self.router.route("financial_extraction")
        with self.assertRaises(FrozenInstanceError):
            route.profile.model = "other"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            route.profile.retry_policy.max_attempts = 9  # type: ignore[misc]

    def test_to_dict_contains_all_required_fields(self) -> None:
        profile = self.router.route("funding_need_analysis").profile
        data = profile.to_dict()
        for key in (
            "provider",
            "model",
            "thinking_enabled",
            "reasoning_effort",
            "max_output_tokens",
            "context_limit",
            "connect_timeout",
            "read_timeout",
            "hard_timeout",
            "retry_policy",
            "concurrency_pool",
            "priority",
            "response_schema",
            "allowed_tools",
            "data_classification",
            "cost_budget",
            "fallback_profile",
            "profile_version",
        ):
            self.assertIn(key, data)
        self.assertIn("max_attempts", data["retry_policy"])

    def test_settings_override_models(self) -> None:
        settings = SimpleNamespace(
            deepseek_flash_model="flash-custom",
            deepseek_pro_model="pro-custom",
        )
        router = ModelRouter(settings)
        self.assertEqual(router.route("financial_extraction").profile.model, "flash-custom")
        self.assertEqual(router.route("evidence_validation").profile.model, "flash-custom")
        self.assertEqual(router.route("critic").profile.model, "pro-custom")

    def test_retry_policy_defaults(self) -> None:
        policy = RetryPolicy()
        self.assertEqual(policy.max_attempts, 2)
        self.assertTrue(policy.retry_on_rate_limit)
        self.assertTrue(policy.retry_on_server_error)
        self.assertFalse(policy.retry_on_timeout)

    def test_financial_extraction_pro_disables_thinking(self) -> None:
        # Regression: Pro-extraction med thinking på + 8000-tak trunkerade
        # JSON (reasoning åt budgeten). Profilen ska ha thinking AV och ett
        # högre output-tak, men fortfarande vara deepseek-pro i analysis_pro.
        route = self.router.route("financial_extraction_pro")
        profile = route.profile
        self.assertEqual(profile.provider, "deepseek")
        self.assertEqual(profile.model, PRO_MODEL)
        self.assertFalse(profile.thinking_enabled)
        self.assertIsNone(profile.reasoning_effort)
        self.assertGreaterEqual(profile.max_output_tokens, 16000)
        self.assertEqual(profile.concurrency_pool, "analysis_pro")

    def test_commentary_pro_keeps_thinking(self) -> None:
        # Commentary/analysis-Pro (funding_need_analysis) ska INTE påverkas
        # av extraction-profiländringen — den behåller thinking + high effort.
        profile = self.router.route("funding_need_analysis").profile
        self.assertTrue(profile.thinking_enabled)
        self.assertEqual(profile.reasoning_effort, "high")


if __name__ == "__main__":
    unittest.main()
