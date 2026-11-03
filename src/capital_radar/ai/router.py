"""Central model routing med immutable execution profiles.

ModelRouter väljer provider, modell och körparametrar för en given task.
Den returnerar en immutable :class:`ExecutionProfile` som följer med genom
hela agent-körningen och sparas i ``agent_runs`` för spårbarhet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Dataklassificeringar för egress-styrning. Allt utom ``PUBLIC`` blockeras
# från extern egress när ``private_data_external_egress_enabled`` är avstängd.
CLASSIFICATION_PUBLIC = "public"
CLASSIFICATION_PRIVATE = "private"
CLASSIFICATION_POTENTIAL_MNPI = "potential_mnpi"
CLASSIFICATION_CONFIRMED_MNPI = "confirmed_mnpi"
DATA_CLASSIFICATIONS: tuple[str, ...] = (
    CLASSIFICATION_PUBLIC,
    CLASSIFICATION_PRIVATE,
    CLASSIFICATION_POTENTIAL_MNPI,
    CLASSIFICATION_CONFIRMED_MNPI,
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_base_seconds: float = 1.0
    retry_on_rate_limit: bool = True
    retry_on_server_error: bool = True
    retry_on_timeout: bool = False


@dataclass(frozen=True)
class ExecutionProfile:
    """Immutable profil för ett agent-run.

    Alla fält som styr ett provider-anrop samlas här så att ett run kan
    reproduceras och kostnadsberäknas i efterhand.
    """

    provider: str
    model: str
    thinking_enabled: bool
    reasoning_effort: str | None
    max_output_tokens: int
    context_limit: int
    connect_timeout: float
    read_timeout: float
    hard_timeout: float
    retry_policy: RetryPolicy
    concurrency_pool: str
    priority: str
    response_schema: str | None = None
    allowed_tools: tuple[str, ...] = ()
    data_classification: str = "public"
    cost_budget: str | None = None
    fallback_profile: str | None = None
    profile_version: str = "v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "context_limit": self.context_limit,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "hard_timeout": self.hard_timeout,
            "retry_policy": {
                "max_attempts": self.retry_policy.max_attempts,
                "backoff_base_seconds": self.retry_policy.backoff_base_seconds,
                "retry_on_rate_limit": self.retry_policy.retry_on_rate_limit,
                "retry_on_server_error": self.retry_policy.retry_on_server_error,
                "retry_on_timeout": self.retry_policy.retry_on_timeout,
            },
            "concurrency_pool": self.concurrency_pool,
            "priority": self.priority,
            "response_schema": self.response_schema,
            "allowed_tools": list(self.allowed_tools),
            "data_classification": self.data_classification,
            "cost_budget": self.cost_budget,
            "fallback_profile": self.fallback_profile,
            "profile_version": self.profile_version,
        }


@dataclass(frozen=True)
class TaskRoute:
    """Routing-beslut för en task."""

    task: str
    profile: ExecutionProfile
    use_ai: bool = True
    reason: str = ""


class ModelRouter:
    """Central routing-tabell för AI-tasks.

    Tabellen är avsiktligt explicit och versionerad. När nya tasks läggs till
    ska de registreras här med ett genomtänkt execution profile.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._routes = self._build_routes()

    def route(self, task: str) -> TaskRoute:
        """Returnera routing-beslut för en task.

        Okända tasks returnerar en säker fallback (no AI) så att inga tokens
        används av misstag.
        """
        route = self._routes.get(task)
        if route is None:
            return TaskRoute(
                task=task,
                profile=self._no_ai_profile(),
                use_ai=False,
                reason=f"okänd task '{task}' — ingen AI-route registrerad",
            )
        return route

    def _build_routes(self) -> dict[str, TaskRoute]:
        s = self._settings
        flash = ExecutionProfile(
            provider="deepseek",
            model=getattr(s, "deepseek_flash_model", "deepseek-v4-flash"),
            thinking_enabled=False,
            reasoning_effort=None,
            max_output_tokens=getattr(
                s, "deepseek_flash_max_output_tokens", 4000
            ),
            context_limit=128000,
            connect_timeout=getattr(
                s, "deepseek_flash_connect_timeout_seconds", 30.0
            ),
            read_timeout=getattr(
                s, "deepseek_flash_read_timeout_seconds", 120.0
            ),
            hard_timeout=getattr(
                s, "deepseek_flash_hard_timeout_seconds", 180.0
            ),
            retry_policy=RetryPolicy(max_attempts=2),
            concurrency_pool="background_flash",
            priority="background_refresh",
            response_schema="extraction",
            data_classification="public",
        )
        pro_high = ExecutionProfile(
            provider="deepseek",
            model=getattr(s, "deepseek_pro_model", "deepseek-v4-flash-vision-exp"),
            thinking_enabled=True,
            reasoning_effort="high",
            max_output_tokens=getattr(
                s, "deepseek_pro_max_output_tokens", 8000
            ),
            context_limit=128000,
            connect_timeout=getattr(
                s, "deepseek_pro_connect_timeout_seconds", 30.0
            ),
            read_timeout=getattr(
                s, "deepseek_pro_read_timeout_seconds", 300.0
            ),
            hard_timeout=getattr(
                s, "deepseek_pro_hard_timeout_seconds", 360.0
            ),
            retry_policy=RetryPolicy(max_attempts=2),
            concurrency_pool="analysis_pro",
            priority="active_deal_analysis",
            response_schema="analysis",
            data_classification="public",
        )
        pro_max = ExecutionProfile(
            provider="deepseek",
            model=getattr(s, "deepseek_pro_model", "deepseek-v4-flash-vision-exp"),
            thinking_enabled=True,
            reasoning_effort="max",
            max_output_tokens=getattr(
                s, "deepseek_pro_max_output_tokens", 8000
            ),
            context_limit=128000,
            connect_timeout=getattr(
                s, "deepseek_pro_connect_timeout_seconds", 30.0
            ),
            read_timeout=getattr(
                s, "deepseek_pro_read_timeout_seconds", 300.0
            ),
            hard_timeout=getattr(
                s, "deepseek_pro_hard_timeout_seconds", 360.0
            ),
            retry_policy=RetryPolicy(max_attempts=2),
            concurrency_pool="critical_pro",
            priority="critical",
            response_schema="analysis",
            data_classification="public",
        )
        pro_chat = ExecutionProfile(
            provider="deepseek",
            model=getattr(s, "deepseek_pro_model", "deepseek-v4-flash-vision-exp"),
            thinking_enabled=True,
            reasoning_effort="high",
            max_output_tokens=getattr(
                s, "deepseek_pro_max_output_tokens", 8000
            ),
            context_limit=128000,
            connect_timeout=getattr(
                s, "deepseek_pro_connect_timeout_seconds", 30.0
            ),
            read_timeout=getattr(
                s, "deepseek_pro_read_timeout_seconds", 300.0
            ),
            hard_timeout=getattr(
                s, "deepseek_pro_hard_timeout_seconds", 360.0
            ),
            retry_policy=RetryPolicy(max_attempts=2),
            concurrency_pool="interactive_chat",
            priority="interactive_chat",
            response_schema="chat",
            data_classification="private",
        )
        # Dedikerad Pro-profil för financial extraction-eskalation.
        #
        # Extraction producerar strikt strukturerad JSON, inte fritt
        # resonemang. Med thinking aktiverat genereras reasoning-tokens FÖRE
        # JSON-svaret; vid high effort + ett 8000-tak åt reasoning upp hela
        # budgeten så att JSON trunkerades mitt i och inte kunde parsas
        # (observerat i produktion som error_type=invalid_output med exakt
        # output_tokens == max_output_tokens). Därför körs extraction-Pro med
        # thinking AV och ett högre output-tak. Detta påverkar INTE
        # commentary/analysis-Pro (pro_high), som behåller thinking.
        pro_extraction = ExecutionProfile(
            provider="deepseek",
            model=getattr(s, "deepseek_pro_model", "deepseek-v4-flash-vision-exp"),
            thinking_enabled=False,
            reasoning_effort=None,
            max_output_tokens=getattr(
                s, "deepseek_pro_extraction_max_output_tokens", 16000
            ),
            context_limit=128000,
            connect_timeout=getattr(
                s, "deepseek_pro_connect_timeout_seconds", 30.0
            ),
            read_timeout=getattr(
                s, "deepseek_pro_read_timeout_seconds", 300.0
            ),
            hard_timeout=getattr(
                s, "deepseek_pro_hard_timeout_seconds", 360.0
            ),
            retry_policy=RetryPolicy(max_attempts=2),
            concurrency_pool="analysis_pro",
            priority="active_deal_analysis",
            response_schema="extraction",
            data_classification="public",
        )
        no_ai = self._no_ai_profile()

        return {
            "universe_filter": TaskRoute(
                task="universe_filter", profile=no_ai, use_ai=False,
                reason="deterministiskt filter",
            ),
            "document_relevance": TaskRoute(
                task="document_relevance", profile=no_ai, use_ai=False,
                reason="deterministiskt först, Kimi low endast om tvetydigt",
            ),
            "financial_extraction": TaskRoute(
                task="financial_extraction", profile=flash,
                reason="strukturerad extraktion, Flash default; eskalera till Pro vid kvalitetsfel",
            ),
            "financial_extraction_pro": TaskRoute(
                task="financial_extraction_pro", profile=pro_extraction,
                reason="eskalation vid kvalitetsfel/ambiguitet i extraktion; thinking av + högre output-tak för att undvika trunkerad JSON",
            ),
            "evidence_validation": TaskRoute(
                task="evidence_validation", profile=flash,
                reason="billig validation, thinking disabled",
            ),
            "ownership_classification": TaskRoute(
                task="ownership_classification", profile=flash,
                reason="klassificering, ej reasoning",
            ),
            "financing_history_normalization": TaskRoute(
                task="financing_history_normalization", profile=flash,
                reason="normalisering, eskalera till Pro vid komplexitet",
            ),
            "funding_need_analysis": TaskRoute(
                task="funding_need_analysis", profile=pro_high,
                reason="djup reasoning",
            ),
            "capital_access_analysis": TaskRoute(
                task="capital_access_analysis", profile=pro_high,
                reason="djup reasoning",
            ),
            "bridge_risk_analysis": TaskRoute(
                task="bridge_risk_analysis", profile=pro_high,
                reason="djup reasoning",
            ),
            "subscription_probability": TaskRoute(
                task="subscription_probability", profile=pro_high,
                reason="djup reasoning",
            ),
            "guarantee_underwriting": TaskRoute(
                task="guarantee_underwriting", profile=pro_high,
                reason="material guarantee case",
            ),
            "position_sizing": TaskRoute(
                task="position_sizing", profile=pro_high,
                reason="deterministisk motor + Pro-tolkning",
            ),
            "critic": TaskRoute(
                task="critic", profile=pro_max,
                reason="red team, högsta reasoning-nivå",
            ),
            "exceptional_escalation": TaskRoute(
                task="exceptional_escalation", profile=pro_max,
                reason="manuell eskalation, högsta reasoning-nivå",
            ),
            "senior_synthesis": TaskRoute(
                task="senior_synthesis", profile=pro_high,
                reason="exekutiv sammanfattning",
            ),
            "executive_chat": TaskRoute(
                task="executive_chat", profile=pro_chat,
                reason="interaktiv chat med tools",
            ),
        }

    @staticmethod
    def _no_ai_profile() -> ExecutionProfile:
        return ExecutionProfile(
            provider="none",
            model="none",
            thinking_enabled=False,
            reasoning_effort=None,
            max_output_tokens=0,
            context_limit=0,
            connect_timeout=0.0,
            read_timeout=0.0,
            hard_timeout=0.0,
            retry_policy=RetryPolicy(max_attempts=0),
            concurrency_pool="background_zero_token",
            priority="background_refresh",
            data_classification="public",
        )
