"""Bounded work pool: asyncio.Queue + N workers + hard timeout.

Detta är den canonical parallella executorn. Etablerade stdlib-mönster:

- ``asyncio.Queue(maxsize=N)``  — begränsar backlog (backpressure)
- ``asyncio.Semaphore``         — begränsar concurrent provider-access
- ``asyncio.timeout``           — outer wall-clock-deadline per work item
- per-item ``try/except``       — failure isolation (ett trasigt jobb
                                  stoppar aldrig batchen)

Queue och semaphore är INTE samma sak: kön begränsar hur mycket som får
ligga och vänta, semaforen begränsar hur många som får prata med
providern samtidigt.

SQLite/job-tabeller är DURABLE STATE; denna kö är bara ACTIVE WORKING
SET. Om processen dör ligger jobben kvar persisted och återtas via
befintlig lease-recovery (``recover_stale_jobs`` m.fl.) — kön bygger
aldrig om durable state i RAM.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .work import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    WorkItem,
    WorkResult,
)

# En handler tar ett WorkItem och returnerar ett WorkResult. Den får aldrig
# kasta — alla expected-fel ska mappas till status. Executorn fångar ändå
# allt som sista skydd (failure isolation).
WorkHandler = Callable[[WorkItem], Awaitable[WorkResult]]


@dataclass
class PoolMetrics:
    """Observerbarhet per pool. Uppdateras av WorkPool, läses av callers."""

    submitted: int = 0
    completed: int = 0
    failed: int = 0
    timed_out: int = 0
    cancelled: int = 0
    rate_limited: int = 0
    validation_failed: int = 0
    skipped_idempotent: int = 0
    provider_calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    latencies_ms: list[int] = field(default_factory=list)

    def record(self, result: WorkResult) -> None:
        if result.status == STATUS_COMPLETED:
            self.completed += 1
        elif result.status == STATUS_TIMEOUT:
            self.timed_out += 1
        elif result.status == STATUS_CANCELLED:
            self.cancelled += 1
        elif result.status == "rate_limited":
            self.rate_limited += 1
        elif result.status == "validation_failed":
            self.validation_failed += 1
        elif result.status == "skipped_idempotent":
            self.skipped_idempotent += 1
        else:
            self.failed += 1
        self.provider_calls += result.provider_calls
        self.input_tokens += result.input_tokens or 0
        self.cached_tokens += result.cached_tokens or 0
        self.output_tokens += result.output_tokens or 0
        if result.latency_ms is not None:
            self.latencies_ms.append(result.latency_ms)

    def latency_percentile(self, percentile: float) -> Optional[float]:
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        index = min(
            len(ordered) - 1,
            max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))),
        )
        return float(ordered[index])

    def snapshot(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "rate_limited": self.rate_limited,
            "validation_failed": self.validation_failed,
            "skipped_idempotent": self.skipped_idempotent,
            "provider_calls": self.provider_calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "latency_p50_ms": self.latency_percentile(50),
            "latency_p95_ms": self.latency_percentile(95),
        }


class WorkPool:
    """En bounded pool för EN work class.

    Parametrar:
        name:        poolens namn (för loggning/metrics)
        handler:     async funktion WorkItem -> WorkResult
        concurrency: antal workers (= max samtidiga items)
        max_queue:   max backlog innan ``submit`` blockerar (backpressure)
        timeout:     outer wall-clock per item (sekunder), utöver ev.
                     SDK/request-timeout inne i handlern
        max_concurrent_provider: semaphore-gräns mot providern (default =
                     samma som concurrency)
    """

    def __init__(
        self,
        *,
        name: str,
        handler: WorkHandler,
        concurrency: int,
        max_queue: int,
        timeout: float,
        max_concurrent_provider: Optional[int] = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency måste vara >= 1")
        if max_queue < concurrency:
            raise ValueError("max_queue måste vara >= concurrency")
        if timeout <= 0:
            raise ValueError("timeout måste vara > 0")
        self.name = name
        self._handler = handler
        self._concurrency = concurrency
        self._timeout = timeout
        self._queue: asyncio.Queue[Optional[WorkItem]] = asyncio.Queue(
            maxsize=max_queue
        )
        self._provider_semaphore = asyncio.Semaphore(
            max_concurrent_provider or concurrency
        )
        self.metrics = PoolMetrics()
        self._workers: list[asyncio.Task[None]] = []
        self._started = False

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def active_workers(self) -> int:
        return sum(1 for worker in self._workers if not worker.done())

    async def __aenter__(self) -> "WorkPool":
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.shutdown()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(
                self._worker_loop(index), name=f"{self.name}-worker-{index}"
            )
            for index in range(self._concurrency)
        ]

    async def submit(self, item: WorkItem) -> None:
        """Lägg ett item i kön. Blockerar när kön är full (backpressure)."""
        if not self._started:
            self.start()
        self.metrics.submitted += 1
        await self._queue.put(item)

    async def _worker_loop(self, index: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:  # poison pill vid shutdown
                    return
                await self._execute(item)
            finally:
                self._queue.task_done()

    async def _execute(self, item: WorkItem) -> None:
        # Failure isolation: allt fångas per item. Ett uncaught exception
        # från ett oberoende jobb cancellar aldrig övriga workers/items.
        started = time.monotonic()
        try:
            async with self._provider_semaphore:
                async with asyncio.timeout(self._timeout):
                    result = await self._handler(item)
        except TimeoutError:
            result = WorkResult(
                work_id=item.work_id,
                status=STATUS_TIMEOUT,
                error_type="hard_timeout",
                error=f"överskred pool-timeout {self._timeout}s",
            )
        except asyncio.CancelledError:
            result = WorkResult(
                work_id=item.work_id,
                status=STATUS_CANCELLED,
                error_type="cancelled",
            )
        except Exception as exc:  # noqa: BLE001 — sista skyddet
            result = WorkResult(
                work_id=item.work_id,
                status=STATUS_FAILED,
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
        if result.latency_ms is None:
            result = WorkResult(
                **{
                    **result.__dict__,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
        self.metrics.record(result)
        item_done = self._on_result
        if item_done is not None:
            item_done(item, result)

    # Valfri callback som sätts av run_batch för att samla resultat.
    _on_result: Optional[Callable[[WorkItem, WorkResult], None]] = None

    async def run_batch(
        self, items: list[WorkItem]
    ) -> list[WorkResult]:
        """Kör en batch och returnera resultat i samma ordning som items.

        Bounded: aldrig fler än ``concurrency`` aktiva samtidigt, aldrig
        fler än ``max_queue`` i kö — ingen task-explosion oavsett
        batch-storlek.
        """
        results: dict[str, WorkResult] = {}

        def _collect(item: WorkItem, result: WorkResult) -> None:
            results[item.work_id] = result

        self._on_result = _collect
        try:
            for item in items:
                await self.submit(item)
            await self._queue.join()
        finally:
            self._on_result = None
        return [results[item.work_id] for item in items]

    async def shutdown(self) -> None:
        """Graceful shutdown: sluta ta nytt arbete, låt kön tömmas, stoppa workers."""
        if not self._started:
            return
        await self._queue.join()
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._started = False
