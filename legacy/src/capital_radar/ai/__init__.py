"""Central AI-provider-abstraktion för Lundbergs Capital Screener.

Lagret separerar business logic från enskilda providers (Moonshot/Kimi,
DeepSeek). Alla providers normaliseras till ett internt response-format
(:class:`ProviderResponse`) så att routing, hälsa, kostnad och circuit
breakers kan hanteras centralt.
"""

from .errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from .response import ProviderResponse, ProviderUsage
from .router import ExecutionProfile, ModelRouter, TaskRoute

__all__ = [
    "AIProvider",
    "ExecutionProfile",
    "ModelRouter",
    "ProviderAuthError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponse",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderUsage",
    "TaskRoute",
]


def __getattr__(name: str):
    # Lazy import för att undvika cirkulära beroenden och hålla
    # importkostnaden nere i CLI-verktyg som inte rör AI.
    if name == "AIProvider":
        from .base import AIProvider

        return AIProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
