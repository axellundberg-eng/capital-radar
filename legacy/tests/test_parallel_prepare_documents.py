"""Tester för parallell dokumenthämtning (MAP 15) och multi-trådad AnalysisWorker.

Verifierar:
- prepare_documents_async: bounded concurrency, failure isolation, oförändrat
  output-kontrakt.
- AnalysisWorker(worker_threads=N): N trådar startas, samma action claimas
  aldrig två gånger, olika actions kan processas parallellt.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from capital_radar.config import Settings
from capital_radar.services import documents as documents_module
from capital_radar.services.documents import prepare_documents_async
from capital_radar.analysis_worker import AnalysisWorker


# ---------------------------------------------------------------------------
# Fakes för prepare_documents
# ---------------------------------------------------------------------------


@dataclass
class _Url:
    url: str = "https://example.com/report.pdf"
    mime_type: str = "application/pdf"
    attachment_id: int | None = None
    content_status: str = "pending"


@dataclass
class _Candidate:
    document_id: int
    issuer_name: str = "Testbolaget"
    title: str = "Delårsrapport"
    urls: list = field(default_factory=lambda: [_Url()])
    issuer_id: int = 1
    profile_category: str = "core"


class _FakeStore:
    """Minimal AnalysisStore-ersättare som fångar save_content-anrop."""

    def __init__(self, candidates, readiness=None):
        self._candidates = candidates
        self._readiness = readiness or {}
        self.saved: list[dict] = []
        self.regraded: list[int] = []
        self._failed_docs: set[int] = set()
        self._lock = threading.Lock()

    def fetch_candidates(self, limit, *, issuer_id=None, prefer_reports=False, document_id=None):
        return list(self._candidates)

    def save_content(self, **kwargs):
        with self._lock:
            self.saved.append(kwargs)
            candidate = kwargs.get("candidate")
            if kwargs.get("status") == "failed" and candidate is not None:
                self._failed_docs.add(candidate.document_id)

    def regrade_document_quality(self, document_id):
        with self._lock:
            self.regraded.append(document_id)

    def document_readiness(self, document_id):
        if document_id in self._failed_docs:
            return {
                "readiness": "failed",
                "reason": "nätverksfel",
                "source_url": None,
                "page_count": 0,
                "char_count": 0,
            }
        return self._readiness.get(
            document_id,
            {
                "readiness": "complete",
                "reason": "ok",
                "source_url": "https://example.com/report.pdf",
                "page_count": 10,
                "char_count": 5000,
            },
        )


class _TrackingFetcher:
    """SafeDocumentFetcher-ersättare som mäter peak-samtidighet över trådar."""

    # Klass-attribut så att instansen som documents.py skapar internt delar
    # räknare med testet (vi sätter dem via monkeypatch på klassen).
    active = 0
    peak = 0
    calls = 0
    delay = 0.05
    fail_on_call: set[int] = set()
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        # Återställ delad state för varje ny pool-körning.
        type(self).active = 0
        type(self).peak = 0
        type(self).calls = 0
        type(self)._lock = threading.Lock()

    def get(self, url: str, **kwargs):
        cls = type(self)
        with cls._lock:
            cls.active += 1
            cls.calls += 1
            call_no = cls.calls
            cls.peak = max(cls.peak, cls.active)
        try:
            time.sleep(cls.delay)
            if call_no in cls.fail_on_call:
                raise documents_module.DocumentError(f"simulerat nätverksfel {url}")
            return SimpleNamespace(
                body=b"%PDF-fake-bytes",
                mime_type="application/pdf",
                final_url=url,
            )
        finally:
            with cls._lock:
                cls.active -= 1


def _settings(**overrides) -> Settings:
    import dataclasses

    base = dict(
        deepseek_api_key="test-key",
        document_allowed_hosts=("example.com",),
        source_fetch_concurrency=4,
        source_fetch_max_queue=32,
        source_fetch_timeout=30.0,
    )
    base.update(overrides)
    return dataclasses.replace(Settings.from_env(), **base)


@pytest.fixture
def mocked_pipeline(monkeypatch):
    """Mocka bort nätverk/OCR/persistens — vi testar schemat, inte PDF-pipelinen."""
    monkeypatch.setattr(documents_module, "SafeDocumentFetcher", _TrackingFetcher)
    monkeypatch.setattr(
        documents_module,
        "extract_document",
        lambda body, mime, final_url: SimpleNamespace(
            text="extraherad text", method="pypdf", status="complete", page_count=10
        ),
    )
    monkeypatch.setattr(
        documents_module,
        "evaluate_document_quality",
        lambda text, **kwargs: SimpleNamespace(score=90, status="good", reason="ok"),
    )
    _TrackingFetcher.fail_on_call = set()
    _TrackingFetcher.delay = 0.05
    return _TrackingFetcher


# ---------------------------------------------------------------------------
# prepare_documents_async
# ---------------------------------------------------------------------------


def test_prepare_documents_bounded_concurrency(mocked_pipeline):
    candidates = [_Candidate(document_id=i) for i in range(1, 13)]
    store = _FakeStore(candidates)
    settings = _settings(source_fetch_concurrency=3)

    import asyncio

    result = asyncio.run(prepare_documents_async(store, settings, limit=12))

    assert result["prepared"] == 12
    assert result["failed"] == 0
    assert len(result["documents"]) == 12
    assert mocked_pipeline.calls == 12
    # Bounded: aldrig fler än 3 samtidiga hämtningar.
    assert mocked_pipeline.peak <= 3
    assert mocked_pipeline.peak >= 2  # parallellism skedde faktiskt
    assert result["executor_metrics"]["submitted"] == 12
    assert result["executor_metrics"]["completed"] == 12


def test_prepare_documents_failure_isolation(mocked_pipeline):
    candidates = [_Candidate(document_id=i) for i in range(1, 6)]
    store = _FakeStore(candidates)
    # Låt exakt ett nätverksanrop misslyckas.
    mocked_pipeline.fail_on_call = {3}
    mocked_pipeline.delay = 0.01
    settings = _settings(source_fetch_concurrency=2)

    import asyncio

    result = asyncio.run(prepare_documents_async(store, settings, limit=5))

    # Ett dokument failar, övriga fyra går igenom — batchen stoppas inte.
    assert result["failed"] == 1
    assert result["prepared"] == 4
    assert len(result["documents"]) == 5
    failed_docs = [d for d in result["documents"] if d["status"] == "failed"]
    assert len(failed_docs) == 1


def test_prepare_documents_output_contract(mocked_pipeline):
    candidates = [_Candidate(document_id=42, issuer_name="Bolaget AB")]
    store = _FakeStore(candidates)

    import asyncio

    result = asyncio.run(prepare_documents_async(store, _settings(), limit=1))

    assert set(result) >= {"seen", "prepared", "needs_ocr", "failed", "documents"}
    doc = result["documents"][0]
    assert doc["document_id"] == 42
    assert doc["issuer"] == "Bolaget AB"
    assert doc["status"] == "complete"
    # save_content anropades för dokumentet med extraherad text.
    assert any(s.get("extracted_text") == "extraherad text" for s in store.saved)
    assert store.regraded == [42]


def test_prepare_documents_empty_candidates(mocked_pipeline):
    store = _FakeStore([])
    import asyncio

    result = asyncio.run(prepare_documents_async(store, _settings(), limit=5))
    assert result["seen"] == 0
    assert result["prepared"] == 0
    assert result["documents"] == []


# ---------------------------------------------------------------------------
# AnalysisWorker med flera trådar
# ---------------------------------------------------------------------------


class _FakeActions:
    """Minimal ManualActionService-ersättare med atomisk claim över trådar."""

    def __init__(self, action_ids, run_delay: float = 0.02):
        self._pending = list(action_ids)
        self._lock = threading.Lock()
        self.claimed: list[int] = []
        self.completed: list[int] = []
        self.run_delay = run_delay

    # --- start/stop-livscykel ---
    def recover_expired_actions(self):
        return {"actions": [], "jobs": []}

    def resume_screening_batches(self):
        return []

    def pending_action_ids(self):
        return list(self._pending)

    # --- claim/run ---
    def claim_action(self, action_id):
        with self._lock:
            if action_id not in self._pending:
                return None
            self._pending.remove(action_id)
            self.claimed.append(action_id)
        return (f"lease-{action_id}", time.time() + 60)

    def run_claimed_action(self, action_id, lease_token, deadline_at):
        time.sleep(self.run_delay)  # simulerat AI-arbete
        with self._lock:
            self.completed.append(action_id)

    def action_status(self, action_id):
        return {
            "issuer_id": 1,
            "status": "completed",
            "phase": "done",
            "result": {"analysis": {"documents": [], "completed": True}},
            "error": None,
        }

    def record_screening_action(self, action_id):
        return None


def _run_worker_briefly(worker: AnalysisWorker, seconds: float = 0.6):
    worker.start()
    time.sleep(seconds)
    worker.stop()


def test_analysis_worker_default_single_thread():
    worker = AnalysisWorker(_FakeActions([]), poll_interval=0.05)
    assert len(worker._workers) == 1
    assert worker._workers[0].name == "bergs-ai-worker-0"


def test_analysis_worker_starts_n_threads():
    worker = AnalysisWorker(_FakeActions([]), poll_interval=0.05, worker_threads=3)
    assert len(worker._workers) == 3
    assert [t.name for t in worker._workers] == [
        "bergs-ai-worker-0", "bergs-ai-worker-1", "bergs-ai-worker-2",
    ]
    _run_worker_briefly(worker, 0.2)
    assert all(not t.is_alive() for t in worker._workers)


def test_analysis_worker_parallel_no_double_claim():
    action_ids = list(range(1, 9))
    actions = _FakeActions(action_ids, run_delay=0.02)
    worker = AnalysisWorker(actions, poll_interval=0.02, worker_threads=4)
    _run_worker_briefly(worker, 0.8)

    # Alla actions claimades exakt en gång (ingen dubbel-claim).
    assert sorted(actions.claimed) == sorted(action_ids)
    assert len(set(actions.claimed)) == len(actions.claimed)
    assert sorted(actions.completed) == sorted(action_ids)


def test_analysis_worker_parallelism_actually_overlaps():
    action_ids = list(range(1, 9))

    def run_until_done(actions, worker_threads):
        worker = AnalysisWorker(actions, poll_interval=0.01, worker_threads=worker_threads)
        worker.start()
        t0 = time.monotonic()
        deadline = t0 + 10.0
        while len(actions.completed) < len(action_ids) and time.monotonic() < deadline:
            time.sleep(0.01)
        duration = time.monotonic() - t0
        worker.stop()
        return duration

    actions_single = _FakeActions(list(action_ids), run_delay=0.05)
    single_duration = run_until_done(actions_single, 1)

    actions_multi = _FakeActions(list(action_ids), run_delay=0.05)
    multi_duration = run_until_done(actions_multi, 4)

    assert len(actions_multi.completed) == 8
    assert len(actions_single.completed) == 8
    # Multi-tråd ska vara klart snabbare på samma arbetsmängd.
    assert multi_duration < single_duration * 0.75
