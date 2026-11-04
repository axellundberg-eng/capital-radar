"""Provider health registry med circuit breakers.

Spårar per-provider hälsa (success/failure, 429, 5xx, latency) och
implementerar en enkel circuit breaker:

    closed  -> normal drift
    open    -> nya lågprioritetsanrop blockeras temporärt
    half_open -> ett probe-anrop släpps igenom efter cooldown

Regler:
- Sparade analyser påverkas aldrig av circuit state.
- interactive_chat- och critical-prioritet släpps alltid igenom.
- Endast 429/5xx räknas som circuit-brytande fel; schemafel och
  ogiltiga requests påverkar inte circuit state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import ProviderRateLimitError, ProviderServerError

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"

# Prioriteter som alltid släpps igenom även när circuit är open.
_ALWAYS_ALLOWED_PRIORITIES = frozenset({"interactive_chat", "critical"})

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


@dataclass
class ProviderHealth:
    """Hälsotillstånd för en provider (eller provider-kanal)."""

    provider: str
    state: str = CIRCUIT_CLOSED
    total_requests: int = 0
    total_successes: int = 0
    total_failures: int = 0
    rate_limit_count: int = 0
    server_error_count: int = 0
    consecutive_breaking_failures: int = 0
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    opened_at: Optional[float] = None
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def average_latency_ms(self) -> Optional[float]:
        if not self.latencies_ms:
            return None
        return sum(self.latencies_ms) / len(self.latencies_ms)

    @property
    def success_rate(self) -> Optional[float]:
        if self.total_requests == 0:
            return None
        return self.total_successes / self.total_requests

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "circuit_state": self.state,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "success_rate": self.success_rate,
            "rate_limit_count": self.rate_limit_count,
            "server_error_count": self.server_error_count,
            "consecutive_breaking_failures": self.consecutive_breaking_failures,
            "last_success_at": self.last_success_at,
            "average_latency_ms": self.average_latency_ms,
        }


class ProviderHealthRegistry:
    """Trådsäker registry över provider-hälsa med circuit breakers."""

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_latency_samples: int = 100,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._max_latency_samples = max_latency_samples
        self._states: dict[str, ProviderHealth] = {}
        self._lock = threading.Lock()

    def _state_for(self, provider: str) -> ProviderHealth:
        state = self._states.get(provider)
        if state is None:
            state = ProviderHealth(provider=provider)
            self._states[provider] = state
        return state

    def allow_request(self, provider: str, *, priority: str = "background") -> bool:
        """Avgör om ett nytt anrop får göras till providern just nu."""
        with self._lock:
            state = self._state_for(provider)
            if state.state == CIRCUIT_CLOSED:
                return True
            if priority in _ALWAYS_ALLOWED_PRIORITIES:
                return True
            if state.state == CIRCUIT_OPEN:
                opened = state.opened_at or 0.0
                if self._clock() - opened >= self._cooldown:
                    state.state = CIRCUIT_HALF_OPEN
                    return True  # probe-anrop
                return False
            # half_open: ett probe-anrop i taget — tillåt och låt
            # record_* flytta state vidare.
            return True

    def record_success(self, provider: str, *, latency_ms: Optional[float] = None) -> None:
        with self._lock:
            state = self._state_for(provider)
            state.total_requests += 1
            state.total_successes += 1
            state.consecutive_breaking_failures = 0
            state.last_success_at = self._clock()
            if latency_ms is not None:
                state.latencies_ms.append(float(latency_ms))
                if len(state.latencies_ms) > self._max_latency_samples:
                    del state.latencies_ms[: -self._max_latency_samples]
            # Lyckat anrop stänger circuit oavsett tidigare state.
            state.state = CIRCUIT_CLOSED
            state.opened_at = None

    def record_failure(self, provider: str, error: BaseException) -> None:
        """Registrera ett misslyckat anrop.

        Endast rate limit (429) och serverfel (5xx) räknas som
        circuit-brytande. Andra fel (schema, auth, request) loggas som
        failures men öppnar inte circuit.
        """
        breaking = isinstance(
            error, (ProviderRateLimitError, ProviderServerError)
        )
        with self._lock:
            state = self._state_for(provider)
            state.total_requests += 1
            state.total_failures += 1
            state.last_failure_at = self._clock()
            if isinstance(error, ProviderRateLimitError):
                state.rate_limit_count += 1
            if isinstance(error, ProviderServerError):
                state.server_error_count += 1
            if not breaking:
                return
            state.consecutive_breaking_failures += 1
            if (
                state.state == CIRCUIT_HALF_OPEN
                or state.consecutive_breaking_failures >= self._failure_threshold
            ):
                state.state = CIRCUIT_OPEN
                state.opened_at = self._clock()

    def circuit_state(self, provider: str) -> str:
        with self._lock:
            return self._state_for(provider).state

    def snapshot(self, provider: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if provider is not None:
                return self._state_for(provider).snapshot()
            return {
                name: state.snapshot() for name, state in self._states.items()
            }


__all__ = [
    "CIRCUIT_CLOSED",
    "CIRCUIT_HALF_OPEN",
    "CIRCUIT_OPEN",
    "ProviderHealth",
    "ProviderHealthRegistry",
]
