from __future__ import annotations

from dataclasses import asdict, dataclass

from ..db import Database
from ..sources.nasdaq import NasdaqUniverseAdapter
from ..sources.ngm import NgmUniverseAdapter


@dataclass(frozen=True)
class UniverseSourceSummary:
    source_id: str
    run_id: int
    securities: int
    inactive: int
    response_hash: str


@dataclass(frozen=True)
class UniverseSyncSummary:
    snapshot_id: int
    sources: tuple[UniverseSourceSummary, ...]

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "sources": [asdict(source) for source in self.sources],
        }


def sync_universe(
    db: Database,
    *,
    nasdaq: NasdaqUniverseAdapter,
    ngm: NgmUniverseAdapter,
    selected_source: str = "all",
) -> UniverseSyncSummary:
    adapters = []
    if selected_source in {"all", "nasdaq"}:
        adapters.append(nasdaq)
    if selected_source in {"all", "ngm"}:
        adapters.append(ngm)
    if not adapters:
        raise ValueError(f"Okänd källa: {selected_source}")

    summaries: list[UniverseSourceSummary] = []
    run_ids: list[int] = []
    for adapter in adapters:
        run_id = db.start_run(adapter.source_id, "universe", adapter.source_url)
        run_ids.append(run_id)
        try:
            result = adapter.fetch()
            for item in result.items:
                db.upsert_security(item, run_id)
            inactive = db.mark_source_securities_inactive_except_run(
                adapter.source_id, [run_id]
            )
            db.finish_run(
                run_id,
                status="success",
                http_status=result.status,
                items_seen=len(result.items),
                items_upserted=len(result.items),
                items_matched=len(result.items),
                response_sha256=result.response_hash,
            )
            summaries.append(
                UniverseSourceSummary(
                    source_id=adapter.source_id,
                    run_id=run_id,
                    securities=len(result.items),
                    inactive=inactive,
                    response_hash=result.response_hash,
                )
            )
        except Exception as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            raise
    snapshot_id = db.create_universe_snapshot(run_ids)
    db.ensure_active_universe_watchlist()
    return UniverseSyncSummary(snapshot_id, tuple(summaries))
