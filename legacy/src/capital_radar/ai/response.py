"""Normaliserat response-format för alla AI-providers.

Varje provider-adapter konverterar sitt API-svar till :class:`ProviderResponse`
innan det lämnar provider-lagret. Business logic ser aldrig rått
provider-specifikt format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderUsage:
    """Tokenförbrukning normaliserad över providers."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ProviderResponse:
    """Internt, provider-oberoende svar från en chat-completion."""

    provider: str
    model: str
    content: str
    finish_reason: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    latency_ms: int | None = None
    request_id: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
