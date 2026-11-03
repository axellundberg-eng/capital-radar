"""Canonical work contract och resultat-taxonomi.

Ett ``WorkItem`` är den minsta enhet dyr/asynkront arbete beskrivs på.
Payloaden ska REFERERA persisted data (t.ex. ``document_id``) snarare än
att duplicera stora dokument genom systemet.

Canonical rule: INGET nätverksanrop får ske innan
- jobbidentitet finns (``work_id``)
- idempotency är kontrollerad
- kostnadsbudget är kontrollerad (``max_provider_calls``)

Status-taxonomin är medvetet liten och delas av alla work classes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# Work classes (logiska pooler, inte specialkodade kanaler).
WORK_SOURCE_FETCH = "source_fetch"
WORK_FLASH_EXTRACTION = "flash_extraction"
WORK_PRO_ANALYSIS = "pro_analysis"
WORK_INTERACTIVE = "interactive"

# Canonical resultat-statusar.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_SKIPPED_IDEMPOTENT = "skipped_idempotent"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_CANCELLED,
        STATUS_RATE_LIMITED,
        STATUS_VALIDATION_FAILED,
        STATUS_SKIPPED_IDEMPOTENT,
    }
)


def _new_work_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class WorkItem:
    """Canonical work item.

    ``max_provider_calls`` är en hård budget: executorn räknar faktiska
    provider-anrop och blockerar call #2 innan nätverk när budgeten är 1.
    """

    kind: str
    handler: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    work_id: str = field(default_factory=_new_work_id)
    priority: int = 1  # 0=high (interactive), 1=normal, 2=low (backfill)
    issuer_id: Optional[int] = None
    document_id: Optional[int] = None
    input_hash: Optional[str] = None
    profile: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None  # monotonic deadline för hela item
    attempt: int = 1
    max_attempts: int = 1
    max_provider_calls: int = 1
    trace_id: Optional[str] = None


@dataclass(frozen=True)
class WorkResult:
    """Canonical resultat från en worker."""

    work_id: str
    status: str
    output: Any = None
    latency_ms: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    provider_calls: int = 0
    error_type: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in {STATUS_COMPLETED, STATUS_SKIPPED_IDEMPOTENT}
