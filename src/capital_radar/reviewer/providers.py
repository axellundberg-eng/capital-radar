"""Provideroberoende gränssnitt för granskaren.

Tjänsten (``service.py``) känner bara till ``ReviewProvider``: ett anrop in
(systemprompt, användarprompt, JSON-schema) och ett ``ProviderResult`` ut med
rå JSON-text och tokens. Validering mot Pydantic sker i tjänsten, så alla
providers bedöms mot exakt samma schema. Claude Haiku är första
implementationen; Grok m.fl. läggs till som egna klasser med samma metod.

Retry-policy, samma som extraction_v2: SDK-retries = 0. Ett granskningsjobb
gör högst ett nätverksanrop. Fel mappas till providerneutrala undantag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

import anthropic


class ProviderError(Exception):
    """Providerfel som inte är timeout eller rate limit."""

    error_type = "provider"

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        if error_type:
            self.error_type = error_type


class ProviderTimeout(ProviderError):
    error_type = "timeout"


class ProviderRateLimited(ProviderError):
    error_type = "rate_limit"


@dataclass(frozen=True)
class ProviderResult:
    text: Optional[str]
    stop_reason: Optional[str]
    response_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_read_tokens: Optional[int] = None


class ReviewProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> ProviderResult: ...

    async def aclose(self) -> None: ...


# USD per miljon tokens (in, ut). Bara för dry-run-estimat och rapportering.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
}


class AnthropicReviewProvider:
    """Claude via Anthropic Messages API med strukturerad output."""

    name = "anthropic"

    def __init__(self, settings: Any, *, client: Any = None) -> None:
        self.model = settings.reviewer_model
        self._effort = settings.reviewer_effort
        self._owns_client = client is None
        self._client = client if client is not None else anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,
            timeout=anthropic.Timeout(
                settings.reviewer_read_timeout_seconds,
                connect=settings.reviewer_connect_timeout_seconds,
            ),
        )

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> ProviderResult:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeout(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                str(exc), error_type=f"provider_http_{exc.status_code}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(str(exc), error_type="connection") from exc

        text = next(
            (block.text for block in response.content if block.type == "text"),
            None,
        )
        usage = getattr(response, "usage", None)
        return ProviderResult(
            text=text,
            stop_reason=getattr(response, "stop_reason", None),
            response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()


def build_provider(settings: Any, *, client: Any = None) -> ReviewProvider | None:
    """Bygg granskarens provider, eller None om granskaren är avstängd."""
    if not getattr(settings, "reviewer_enabled", False):
        return None
    return AnthropicReviewProvider(settings, client=client)
