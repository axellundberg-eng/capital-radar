"""Tester för canonical WorkPool (execution-lagret).

Täcker DONE WHEN-kraven:
- bounded concurrency (aldrig fler än N samtidiga)
- queue/backpressure (max_queue respekteras)
- failure isolation (ett kastande jobb stoppar inte batchen)
- hard timeout (långsam handler → status timeout, worker fri)
- cancellation (cancelled item → status cancelled)
- metrics (provider calls/tokens/latency kan räknas exakt)
"""

from __future__ import annotations

import asyncio
import unittest

from capital_radar.execution import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    WorkItem,
    WorkPool,
    WorkResult,
)


def _item(name: str) -> WorkItem:
    return WorkItem(
        kind="flash_extraction", handler="test", payload={"name": name}
    )


def _ok_result(item: WorkItem, **extra) -> WorkResult:
    return WorkResult(work_id=item.work_id, status=STATUS_COMPLETED, **extra)


class WorkPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_concurrency(self) -> None:
        active = 0
        peak = 0

        async def handler(item: WorkItem) -> WorkResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return _ok_result(item)

        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=3,
            max_queue=16,
            timeout=5.0,
        ) as pool:
            results = await pool.run_batch([_item(f"i{n}") for n in range(12)])

        self.assertEqual(len(results), 12)
        self.assertTrue(all(r.status == STATUS_COMPLETED for r in results))
        self.assertLessEqual(peak, 3, "aldrig fler än concurrency samtidiga")
        self.assertGreater(peak, 1, "ska faktiskt köra parallellt")

    async def test_failure_isolation(self) -> None:
        async def handler(item: WorkItem) -> WorkResult:
            if item.payload["name"] == "boom":
                raise RuntimeError("trasigt dokument")
            return _ok_result(item)

        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=2,
            max_queue=8,
            timeout=5.0,
        ) as pool:
            results = await pool.run_batch(
                [_item("a"), _item("boom"), _item("b"), _item("c")]
            )

        by_name = {
            item.payload["name"]: result
            for item, result in zip(
                [_item("a"), _item("boom"), _item("b"), _item("c")], results
            )
        }
        statuses = [r.status for r in results]
        self.assertEqual(statuses.count(STATUS_COMPLETED), 3)
        self.assertEqual(statuses.count(STATUS_FAILED), 1)
        failed = next(r for r in results if r.status == STATUS_FAILED)
        self.assertEqual(failed.error_type, "RuntimeError")
        self.assertIn("trasigt dokument", failed.error or "")

    async def test_hard_timeout_frees_worker(self) -> None:
        async def handler(item: WorkItem) -> WorkResult:
            if item.payload["name"] == "slow":
                await asyncio.sleep(10)
            return _ok_result(item)

        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=2,
            max_queue=8,
            timeout=0.1,
        ) as pool:
            results = await pool.run_batch(
                [_item("slow"), _item("fast1"), _item("fast2")]
            )

        statuses = sorted(r.status for r in results)
        self.assertEqual(
            statuses, sorted([STATUS_TIMEOUT, STATUS_COMPLETED, STATUS_COMPLETED])
        )
        self.assertEqual(pool.metrics.timed_out, 1)

    async def test_cancellation_maps_to_cancelled(self) -> None:
        async def handler(item: WorkItem) -> WorkResult:
            if item.payload["name"] == "cancel":
                raise asyncio.CancelledError()
            return _ok_result(item)

        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=2,
            max_queue=8,
            timeout=5.0,
        ) as pool:
            results = await pool.run_batch([_item("cancel"), _item("ok")])

        statuses = sorted(r.status for r in results)
        self.assertEqual(statuses, sorted([STATUS_CANCELLED, STATUS_COMPLETED]))

    async def test_backpressure_queue_bounded(self) -> None:
        release = asyncio.Event()

        async def handler(item: WorkItem) -> WorkResult:
            await release.wait()
            return _ok_result(item)

        pool = WorkPool(
            name="t", handler=handler, concurrency=1, max_queue=2, timeout=5.0
        )
        pool.start()
        try:
            await pool.submit(_item("a"))  # tas av worker
            await pool.submit(_item("b"))  # i kö
            await asyncio.sleep(0.05)
            self.assertLessEqual(pool.queue_depth, 2)
            release.set()
            await pool._queue.join()
        finally:
            release.set()
            await pool.shutdown()

    async def test_metrics_exact_counting(self) -> None:
        async def handler(item: WorkItem) -> WorkResult:
            return _ok_result(
                item,
                provider_calls=1,
                input_tokens=100,
                cached_tokens=40,
                output_tokens=25,
            )

        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=4,
            max_queue=16,
            timeout=5.0,
        ) as pool:
            await pool.run_batch([_item(f"i{n}") for n in range(7)])
            snap = pool.metrics.snapshot()

        self.assertEqual(snap["submitted"], 7)
        self.assertEqual(snap["completed"], 7)
        self.assertEqual(snap["provider_calls"], 7)
        self.assertEqual(snap["input_tokens"], 700)
        self.assertEqual(snap["cached_tokens"], 280)
        self.assertEqual(snap["output_tokens"], 175)
        self.assertIsNotNone(snap["latency_p50_ms"])

    async def test_result_order_matches_input(self) -> None:
        async def handler(item: WorkItem) -> WorkResult:
            # Varierad latens — ordningen ska ändå följa input.
            await asyncio.sleep(0.01 * (3 - len(item.payload["name"]) % 3))
            return _ok_result(item, output=item.payload["name"])

        names = [f"n{n}" for n in range(10)]
        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=4,
            max_queue=16,
            timeout=5.0,
        ) as pool:
            results = await pool.run_batch([_item(n) for n in names])

        self.assertEqual([r.output for r in results], names)

    async def test_parallel_speedup(self) -> None:
        """20 jobs à 50ms med concurrency 4 ska ta ~4x kortare än sekventiellt."""
        import time

        async def handler(item: WorkItem) -> WorkResult:
            await asyncio.sleep(0.05)
            return _ok_result(item)

        start = time.monotonic()
        async with WorkPool(
            name="t",
            handler=handler,
            concurrency=4,
            max_queue=32,
            timeout=5.0,
        ) as pool:
            await pool.run_batch([_item(f"i{n}") for n in range(20)])
        elapsed = time.monotonic() - start

        sequential = 20 * 0.05
        self.assertLess(
            elapsed,
            sequential * 0.6,
            f"parallell körning ({elapsed:.2f}s) ska slå sekventiell ({sequential:.2f}s)",
        )


if __name__ == "__main__":
    unittest.main()
