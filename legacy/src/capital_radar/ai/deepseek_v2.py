"""AsyncOpenAI-transport och execution-profiler för DeepSeek V2.

Detta är den ENDA platsen där HTTP/transport för AI-anrop konfigureras i
den nya runtime:n. Vi använder official ``openai.AsyncOpenAI`` — ingen
custom HTTP-klient, ingen urllib, ingen egen connection pooling, ingen
egen request-serialisering.

``max_retries=0`` är avsiktligt: OpenAI SDK har annars egna automatiska
retries, och vi vill att ALL retry-logik ska vara explicit och observerbar
(i Instructor/AgentRunner), inte dold i transportlagret.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import openai

from .base import AIProvider
from .errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from .response import ProviderResponse, ProviderUsage

# Profiler enligt mandat punkt 11. Extraction använder ALDRIG analysis-profil.
PROFILE_FLASH_EXTRACTION = "flash_extraction"
PROFILE_PRO_EXTRACTION = "pro_extraction"
PROFILE_PRO_ANALYSIS = "pro_analysis"
PROFILE_PRO_CHAT = "pro_chat"
PROFILE_CRITICAL_PRO = "critical_pro"


@dataclass(frozen=True)
class V2Profile:
    """Immutable profil för ett V2-anrop."""

    name: str
    model: str
    thinking: bool
    reasoning_effort: Optional[str]
    max_output_tokens: int
    timeout_seconds: float


def build_async_client(settings: Any) -> openai.AsyncOpenAI:
    """Bygg en AsyncOpenAI-klient för DeepSeek.

    ``max_retries=0`` gör att SDK:n inte gör egna retries — all retry är
    explicit och observerbar högre upp i stacken. ``timeout`` sätts per
    profil vid anropet, inte här, så att Flash och Pro kan ha olika tak.
    """
    api_key = getattr(settings, "deepseek_api_key", None)
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY saknas — V2 extraction kan inte starta")
    base_url = getattr(settings, "deepseek_base_url", "https://api.deepseek.com")
    return openai.AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
    )


def resolve_profile(settings: Any, name: str) -> V2Profile:
    """Resolve en V2-profil från settings.

    Modellnamn och token-tak kommer från settings (env-styrda) — inget
    hårdkodas här. Detta speglar mandatets profilsplit:

    - FLASH_EXTRACTION: flash, thinking=false, 8192 output
    - PRO_EXTRACTION:   pro, thinking=false, 16000 output (strikt JSON)
    - PRO_ANALYSIS:     pro, thinking=true, high
    - PRO_CHAT:         pro, thinking=true
    - CRITICAL_PRO:     pro, thinking=true, max
    """
    flash_model = getattr(settings, "deepseek_flash_model", "deepseek-v4-flash")
    pro_model = getattr(settings, "deepseek_pro_model", "deepseek-v4-flash-vision-exp")
    flash_tokens = int(getattr(settings, "deepseek_flash_max_output_tokens", 8192))
    pro_extraction_tokens = int(
        getattr(settings, "deepseek_pro_extraction_max_output_tokens", 16000)
    )
    pro_tokens = int(getattr(settings, "deepseek_pro_max_output_tokens", 8192))
    flash_timeout = float(
        getattr(settings, "deepseek_flash_read_timeout_seconds", 120.0)
    )
    pro_timeout = float(getattr(settings, "deepseek_pro_read_timeout_seconds", 300.0))

    profiles = {
        PROFILE_FLASH_EXTRACTION: V2Profile(
            name=name,
            model=flash_model,
            thinking=False,
            reasoning_effort=None,
            max_output_tokens=flash_tokens,
            timeout_seconds=flash_timeout,
        ),
        PROFILE_PRO_EXTRACTION: V2Profile(
            name=name,
            model=pro_model,
            thinking=False,
            reasoning_effort=None,
            max_output_tokens=pro_extraction_tokens,
            timeout_seconds=pro_timeout,
        ),
        PROFILE_PRO_ANALYSIS: V2Profile(
            name=name,
            model=pro_model,
            thinking=True,
            reasoning_effort="high",
            max_output_tokens=pro_tokens,
            timeout_seconds=pro_timeout,
        ),
        PROFILE_PRO_CHAT: V2Profile(
            name=name,
            model=pro_model,
            thinking=True,
            reasoning_effort=None,
            max_output_tokens=pro_tokens,
            timeout_seconds=pro_timeout,
        ),
        PROFILE_CRITICAL_PRO: V2Profile(
            name=name,
            model=pro_model,
            thinking=True,
            reasoning_effort="max",
            max_output_tokens=pro_tokens,
            timeout_seconds=pro_timeout,
        ),
    }
    profile = profiles.get(name)
    if profile is None:
        raise ValueError(f"okänd V2-profil '{name}'")
    return profile


# Tråd-lokal event-loop-hållare. ``asyncio.run`` skapar och förstör en loop per
# anrop, men AsyncOpenAI:s underliggande httpx-klient är bunden till sin loop.
# Genom att återanvända en loop per tråd undviker vi "attached to a different
# loop"-fel när samma klient anropas flera gånger från samma worker-tråd.
_thread_local = threading.local()


def _run_async(coro: Any) -> Any:
    """Kör en coroutine synkront på en tråd-lokal event loop."""
    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_local.loop = loop
    return loop.run_until_complete(coro)


def _map_openai_error(exc: Exception) -> ProviderError:
    """Mappa ett OpenAI SDK-exception till vår ProviderError-hierarki.

    AgentRunner:s retry-policy styrs av dessa typer: rate limit och serverfel
    retrias, timeout retrias aldrig (retry_on_timeout=False), och övriga fel
    (auth, ogiltig request, schemafel) bryter direkt utan blind retry.
    """
    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimitError(f"DeepSeek rate limit: {exc}")
    if isinstance(exc, openai.AuthenticationError):
        return ProviderAuthError(f"DeepSeek auth failed: {exc}")
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError(f"DeepSeek timeout: {exc}")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is not None and status >= 500:
            return ProviderServerError(f"DeepSeek server error {status}: {exc}")
        return ProviderError(f"DeepSeek API error {status}: {exc}")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError(f"DeepSeek connection failed: {exc}")
    return ProviderError(f"DeepSeek request failed: {exc}")


class DeepSeekV2Provider:
    """Synkron ``AIProvider`` som internt kör ``openai.AsyncOpenAI``.

    Detta är den canonical sync-bryggan för AgentRunner-baserade flöden
    (t.ex. news commentary). Den ersätter den gamla urllib-klienten i
    ``ai/deepseek.py``: transport, connection pooling, request-serialisering
    och standard-API-exceptions hanteras av official ``openai`` SDK — vi
    skriver ingen egen HTTP-kod.

    ``structured_completion`` matchar AgentRunner:s kanoniska kwargs och
    returnerar en normaliserad ``ProviderResponse``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        thinking_enabled: bool,
        reasoning_effort: Optional[str],
        max_output_tokens: int,
        read_timeout_seconds: float,
        provider_label: str,
    ) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,  # all retry är explicit och observerbar högre upp
        )
        self._model = model
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._read_timeout = read_timeout_seconds
        self._provider_label = provider_label

    @property
    def provider_name(self) -> str:
        return self._provider_label

    async def _call_async(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int,
        thinking: bool,
        reasoning_effort: Optional[str],
        timeout: float,
    ) -> Any:
        extra_body: dict[str, Any] = {
            "thinking": {"type": "enabled" if thinking else "disabled"}
        }
        if thinking and reasoning_effort:
            extra_body["reasoning_effort"] = reasoning_effort
        return await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
            timeout=timeout,
            extra_body=extra_body,
        )

    def structured_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: Mapping[str, Any],
        schema_name: str = "",
        prompt_version: str = "",
        max_output_tokens: Optional[int] = None,
        thinking: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
        request_metadata: Optional[Mapping[str, Any]] = None,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        messages: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> ProviderResponse:
        # ``schema``/``schema_name``/``prompt_version``/``request_metadata``
        # används för spårbarhet i övre lager (AgentRunner) och skickas inte i
        # payloaden. ``prompt_cache_key`` mappas inte — DeepSeek cachar automatiskt.
        effective_thinking = (
            self._thinking_enabled if thinking is None else thinking == "enabled"
        )
        effective_effort = (
            self._reasoning_effort if reasoning_effort is None else reasoning_effort
        )
        if not effective_thinking:
            effective_effort = None
        effective_max_tokens = int(max_output_tokens or self._max_output_tokens)

        if messages is not None:
            message_list = [dict(m) for m in messages]
        else:
            message_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        started = time.monotonic()
        try:
            completion = _run_async(
                self._call_async(
                    messages=message_list,
                    max_tokens=effective_max_tokens,
                    thinking=effective_thinking,
                    reasoning_effort=effective_effort,
                    timeout=self._read_timeout,
                )
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise _map_openai_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return _normalize_completion(
            completion, model=self._model, latency_ms=latency_ms
        )

    def health_check(self) -> dict[str, Any]:
        return {"configured": True}


def _normalize_completion(
    completion: Any, *, model: str, latency_ms: int
) -> ProviderResponse:
    """Normalisera en OpenAI chat-completion till ``ProviderResponse``."""
    choice = completion.choices[0] if completion.choices else None
    message = getattr(choice, "message", None) if choice is not None else None
    content = getattr(message, "content", None) if message is not None else None
    finish_reason = getattr(choice, "finish_reason", None) if choice is not None else None

    usage_raw = getattr(completion, "usage", None)
    cached = None
    if usage_raw is not None:
        details = getattr(usage_raw, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", None)
    usage = ProviderUsage(
        input_tokens=getattr(usage_raw, "prompt_tokens", None),
        cached_input_tokens=cached,
        output_tokens=getattr(usage_raw, "completion_tokens", None),
        total_tokens=getattr(usage_raw, "total_tokens", None),
    )
    return ProviderResponse(
        provider="deepseek",
        model=str(getattr(completion, "model", None) or model),
        content=content if isinstance(content, str) else "",
        finish_reason=str(finish_reason or ""),
        usage=usage,
        latency_ms=latency_ms,
        request_id=str(getattr(completion, "id", None) or "") or None,
    )


def build_deepseek_v2_provider(
    settings: Any,
    *,
    channel: str,
) -> Optional[DeepSeekV2Provider]:
    """Bygg en sync DeepSeek V2-provider, eller ``None`` om okonfigurerad.

    ``channel`` är ``"flash"`` eller ``"pro"``; båda delar API-nyckel och
    base_url men har oberoende modell/thinking/timeout-konfiguration. Detta är
    drop-in-ersättningen för den borttagna ``ai/deepseek.build_deepseek_provider``.
    """
    api_key = str(getattr(settings, "deepseek_api_key", "") or "").strip()
    if not api_key:
        return None
    base_url = str(
        getattr(settings, "deepseek_base_url", "https://api.deepseek.com")
        or "https://api.deepseek.com"
    )
    if channel == "flash":
        model = str(getattr(settings, "deepseek_flash_model", "deepseek-v4-flash"))
        thinking = bool(getattr(settings, "deepseek_flash_thinking", False))
        reasoning: Optional[str] = None
        max_tokens = int(getattr(settings, "deepseek_flash_max_output_tokens", 4096))
        read_timeout = float(
            getattr(settings, "deepseek_flash_read_timeout_seconds", 120.0)
        )
    else:
        model = str(getattr(settings, "deepseek_pro_model", "deepseek-v4-flash-vision-exp"))
        thinking = bool(getattr(settings, "deepseek_pro_thinking", True))
        reasoning = getattr(settings, "deepseek_pro_reasoning", "high")
        max_tokens = int(getattr(settings, "deepseek_pro_max_output_tokens", 8192))
        read_timeout = float(
            getattr(settings, "deepseek_pro_read_timeout_seconds", 300.0)
        )
    if not thinking:
        reasoning = None
    return DeepSeekV2Provider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        thinking_enabled=thinking,
        reasoning_effort=reasoning,
        max_output_tokens=max_tokens,
        read_timeout_seconds=read_timeout,
        provider_label=f"deepseek-{channel}",
    )


__all__ = [
    "PROFILE_CRITICAL_PRO",
    "PROFILE_FLASH_EXTRACTION",
    "PROFILE_PRO_ANALYSIS",
    "PROFILE_PRO_CHAT",
    "PROFILE_PRO_EXTRACTION",
    "DeepSeekV2Provider",
    "V2Profile",
    "build_async_client",
    "build_deepseek_v2_provider",
    "resolve_profile",
]
