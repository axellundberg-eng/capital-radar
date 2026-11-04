"""Basklass för AI-providers."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .response import ProviderResponse


@runtime_checkable
class AIProvider(Protocol):
    """Kontrakt för en runtime AI-provider.

    Implementationer ska vara trådsäkra och får inte lagra API-nycklar i
    loggbara attribut. Alla metoder ska returnera normaliserade
    :class:`ProviderResponse` eller kasta :class:`ProviderError`-subklasser.

    Det kanoniska anropsgränssnittet är :meth:`structured_completion` nedan.
    ``AgentRunner`` anropar alltid providers med exakt dessa kwargs — inga
    andra namn får användas av anropare eller implementationer.
    """

    @property
    def provider_name(self) -> str:
        """Kort identifierare, t.ex. ``moonshot`` eller ``deepseek``."""
        ...

    def structured_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, Any],
        schema_name: str,
        prompt_version: str,
        max_output_tokens: int | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        prompt_cache_key: str | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> ProviderResponse:
        """Kör en strukturerad completion och returnera normaliserat svar.

        Kanoniska kwargs (används identiskt av Protocol, AgentRunner och
        provider-implementationer):

        - ``schema`` — JSON-schema för förväntat svar.
        - ``schema_name`` — namn på schemat (för spårbarhet/prompt).
        - ``prompt_version`` — version av prompten (för spårbarhet).
        - ``thinking`` — t.ex. ``"enabled"``/``"disabled"`` eller ``None``.
        - ``reasoning_effort`` — t.ex. ``"high"``/``"max"`` eller ``None``.
        - ``prompt_cache_key`` — cache-nyckel eller ``None``.
        - ``request_metadata`` — extra metadata eller ``None``.
        - ``tools`` — tool-definitioner eller ``None``.
        - ``messages`` — färdig meddelandelista; när den sätts ska den
          användas i stället för ``system_prompt``/``user_prompt``.
        """
        ...

    def health_check(self) -> dict[str, Any]:
        """Returnera hälsostatus utan att göra ett kostsamt anrop.

        Ska innehålla minst ``configured`` (bool) och kan innehålla
        ``circuit_state``, ``last_success_at`` m.m.
        """
        ...
