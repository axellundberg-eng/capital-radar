"""Provider-specifika feltyper.

Dessa fel är avsedda att fångas av :class:`~screener.ai.base.AIProvider`
och mappas till normaliserade undantag så att övre lager inte behöver känna
 till enskilda providers HTTP-detaljer.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Basklass för alla provider-fel."""


class ProviderUnavailableError(ProviderError):
    """Providern är inte konfigurerad (t.ex. saknad API-nyckel)."""


class ProviderAuthError(ProviderError):
    """Autentisering misslyckades (401/403)."""


class ProviderRateLimitError(ProviderError):
    """Rate limit (429)."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderServerError(ProviderError):
    """Serverfel hos providern (5xx)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderTimeoutError(ProviderError):
    """Request- eller read-timeout."""

    def __init__(
        self,
        message: str,
        *,
        timeout_source: str = "socket_or_read_timeout",
        elapsed_seconds: float | None = None,
    ):
        super().__init__(message)
        self.timeout_source = timeout_source
        self.elapsed_seconds = elapsed_seconds
