"""Canonical execution-lager för asynkront arbete.

Detta paket är den ENDA platsen där concurrency, bounded queues, outer
wall-clock-timeouts och per-jobb failure-isolation implementeras för
API/AI-flöden. Features (extraction, framtida analys/chat) ska vara
feature-specifika i prompt/schema/validering/persistens — inte i
transport, event-loop-hantering, retry eller concurrency.

Koncept:

- ``WorkItem``      — canonical work contract (identitet, budget, deadline)
- ``WorkResult``    — canonical resultat-taxonomi + metadata
- ``WorkPool``      — bounded ``asyncio.Queue`` + N workers per work class
- ``PoolMetrics``   — observerbarhet (queue depth, latency, tokens, fel)
"""

from .work import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RATE_LIMITED,
    STATUS_SKIPPED_IDEMPOTENT,
    STATUS_TIMEOUT,
    STATUS_VALIDATION_FAILED,
    TERMINAL_STATUSES,
    WORK_FLASH_EXTRACTION,
    WORK_INTERACTIVE,
    WORK_PRO_ANALYSIS,
    WORK_SOURCE_FETCH,
    WorkItem,
    WorkResult,
)
from .pool import PoolMetrics, WorkPool

__all__ = [
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RATE_LIMITED",
    "STATUS_SKIPPED_IDEMPOTENT",
    "STATUS_TIMEOUT",
    "STATUS_VALIDATION_FAILED",
    "TERMINAL_STATUSES",
    "WORK_FLASH_EXTRACTION",
    "WORK_INTERACTIVE",
    "WORK_PRO_ANALYSIS",
    "WORK_SOURCE_FETCH",
    "PoolMetrics",
    "WorkItem",
    "WorkPool",
    "WorkResult",
]
