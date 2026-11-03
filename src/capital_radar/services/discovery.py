from __future__ import annotations

from dataclasses import asdict, dataclass

from ..db import Database
from ..sources.issuer_feeds import IssuerFeedAdapter
from ..sources.mfn import MfnDiscoveryAdapter
from ..sources.nasdaq_news import NasdaqNewsAdapter


@dataclass(frozen=True)
class DiscoverySourceSummary:
    source_id: str
    run_id: int
    seen: int
    matched: int
    created: int
    linked: int
    queued: int
    unmatched: int


@dataclass(frozen=True)
class DiscoverySummary:
    sources: tuple[DiscoverySourceSummary, ...]

    def to_dict(self) -> dict:
        return {"sources": [asdict(source) for source in self.sources]}


def discover_news(
    db: Database,
    *,
    mfn: MfnDiscoveryAdapter,
    nasdaq: NasdaqNewsAdapter,
    issuer_ir: IssuerFeedAdapter,
    selected_source: str = "all",
    mfn_limit: int | None = None,
    nasdaq_limit: int = 250,
) -> DiscoverySummary:
    jobs: list[tuple[object, dict]] = []
    if selected_source in {"all", "mfn"}:
        jobs.append((mfn, {"limit": mfn_limit}))
    if selected_source in {"all", "nasdaq"}:
        jobs.append((nasdaq, {"max_items": nasdaq_limit}))
    if selected_source in {"all", "ir"}:
        jobs.append((issuer_ir, {}))
    if not jobs:
        raise ValueError(f"Okänd nyhetskälla: {selected_source}")

    summaries: list[DiscoverySourceSummary] = []
    for adapter, kwargs in jobs:
        run_id = db.start_run(adapter.source_id, "news_discovery", adapter.source_url)
        try:
            result = adapter.fetch(**kwargs)
            created = linked = queued = unmatched = 0
            for item in result.items:
                outcome, should_queue = db.upsert_document(item, universe_only=True)
                if outcome == "unmatched":
                    unmatched += 1
                elif outcome == "created":
                    created += 1
                else:
                    linked += 1
                if outcome != "unmatched" and should_queue:
                    queued += 1
            matched = created + linked
            db.finish_run(
                run_id,
                status="success",
                http_status=result.status,
                items_seen=len(result.items),
                items_upserted=created,
                items_matched=matched,
                items_queued=queued,
                response_sha256=result.response_hash,
            )
            summaries.append(
                DiscoverySourceSummary(
                    source_id=adapter.source_id,
                    run_id=run_id,
                    seen=len(result.items),
                    matched=matched,
                    created=created,
                    linked=linked,
                    queued=queued,
                    unmatched=unmatched,
                )
            )
        except Exception as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            raise
    return DiscoverySummary(tuple(summaries))
