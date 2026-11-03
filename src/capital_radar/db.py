"""SQLite-lagret.

Schemat i `schema.sql` är fryst från Screener-analys-hus och skapas med
IF NOT EXISTS, så att produktionsdatabasen kan öppnas oförändrad. Kolumner
som finns i schemat men saknas i en äldre databas läggs till automatiskt.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator

from .models import DocumentCandidate, SecurityCandidate
from .mfn_identity import known_mfn_issuer
from .normalization import (
    canonicalize_url,
    document_event_key,
    issuer_display_name,
    normalize_issuer_name,
    stable_hash,
    utc_now,
)
from .projection_revision import ensure_projection_revision_schema
from .relevance import classify_title


def schema_sql() -> str:
    return files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


SOURCE_SEED = (
    {
        "id": "nasdaq_nordic",
        "name": "Nasdaq Nordic Market Data",
        "source_kind": "exchange_universe",
        "base_url": "https://api.nasdaq.com/api/nordic",
        "evidence_tier": 1,
        "authority": "official_exchange",
        "access_status": "verified_public",
        "terms_status": "public_web_endpoint_review_before_production",
        "notes": "Small Cap Stockholm samt First North GM/Premier Sweden.",
    },
    {
        "id": "ngm",
        "name": "Nordic Growth Market",
        "source_kind": "exchange_universe",
        "base_url": "https://ngm-api-prod.vmate.se",
        "evidence_tier": 1,
        "authority": "official_exchange_frontend_api",
        "access_status": "verified_public",
        "terms_status": "public_web_endpoint_review_before_production",
        "notes": "NGM Growth Market, tidigare NGM Nordic SME.",
    },
    {
        "id": "mfn",
        "name": "MFN",
        "source_kind": "news_aggregator",
        "base_url": "https://www.mfn.se",
        "evidence_tier": 3,
        "authority": "aggregator_discovery_only",
        "access_status": "verified_public_html",
        "terms_status": "robots_allows_article_paths_commercial_feed_to_clarify",
        "notes": "Metadata-discovery. Originalrapport/issuer IR styr finansiella fakta.",
    },
    {
        "id": "nasdaq_cns",
        "name": "Nasdaq Company News Service",
        "source_kind": "exchange_news",
        "base_url": "https://api.news.eu.nasdaq.com/news",
        "evidence_tier": 2,
        "authority": "official_exchange_news",
        "access_status": "verified_public_jsonp",
        "terms_status": "public_web_endpoint_review_before_production",
        "notes": "Oberoende reservflöde till MFN för Nasdaq-listade bolag.",
    },
    {
        "id": "issuer_ir",
        "name": "Issuer IR feeds",
        "source_kind": "issuer_original_news",
        "base_url": "configured-per-issuer",
        "evidence_tier": 1,
        "authority": "issuer_original",
        "access_status": "adapter_ready_configuration_empty",
        "terms_status": "verify_per_configured_feed",
        "notes": "RSS/Atom-reservflöden från bolagens egna IR-sidor.",
    },
)


def is_sqlite_busy_error(exc: BaseException) -> bool:
    """Returnera True endast för tillfälliga SQLite-låsfel.

    Matchar 'database is locked' och 'database is busy'. Alla andra
    OperationalError (schemafel, syntaxfel, readonly m.m.) ska re-raisas
    och får inte döljas eller loggas som låsfel.
    """
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # WAL låter läsare och en skrivare arbeta samtidigt. Utan WAL tar
        # varje skrivtransaktion ett EXCLUSIVE-lås på hela filen, vilket
        # blockerar watchdogs och HTTP-läsare tills busy_timeout löper ut
        # ("database is locked"). Läget är persistent i DB-filen.
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = schema_sql()
        with self.connect() as connection:
            connection.executescript(schema)
            self._add_missing_columns(connection, schema)
            now = utc_now()
            # Äldre databaser kan sakna statusen 'blocked' i agent_runs.
            self._migrate_agent_runs_blocked_status(connection, now)
            for source in SOURCE_SEED:
                connection.execute(
                    """
                    INSERT INTO sources (
                        id, name, source_kind, base_url, evidence_tier, authority,
                        access_status, terms_status, notes, last_verified_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :name, :source_kind, :base_url, :evidence_tier,
                        :authority, :access_status, :terms_status, :notes,
                        :last_verified_at, :created_at, :updated_at
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        source_kind=excluded.source_kind,
                        base_url=excluded.base_url,
                        evidence_tier=excluded.evidence_tier,
                        authority=excluded.authority,
                        access_status=excluded.access_status,
                        terms_status=excluded.terms_status,
                        notes=excluded.notes,
                        last_verified_at=excluded.last_verified_at,
                        updated_at=excluded.updated_at
                    """,
                    {
                        **source,
                        "last_verified_at": "2026-07-23",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            # Sist: läsmodellens revisionstriggers. De versionshanteras av
            # projection_revision och ligger därför inte i schema.sql.
            ensure_projection_revision_schema(connection, applied_at=now)

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection, schema: str) -> None:
        """Lägg till kolumner som en äldre databas saknar.

        Jämför mot en ren minnesdatabas byggd från samma schema. Kolumner som
        är NOT NULL utan default kan inte läggas till säkert och ger fel.
        """
        with closing(sqlite3.connect(":memory:")) as reference:
            reference.executescript(schema)
            tables = [
                name for (name,) in reference.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for table in tables:
                existing = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for _cid, name, declared, notnull, default, pk in reference.execute(
                    f"PRAGMA table_info({table})"
                ):
                    if name in existing:
                        continue
                    if pk or (notnull and default is None):
                        raise RuntimeError(
                            f"Kan inte lägga till {table}.{name} automatiskt"
                        )
                    definition = f"{name} {declared}".strip()
                    if notnull:
                        definition += " NOT NULL"
                    if default is not None:
                        definition += f" DEFAULT {default}"
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @staticmethod
    def _migrate_agent_runs_blocked_status(
        connection: sqlite3.Connection,
        now: str,
    ) -> None:
        """Allow the persisted ``blocked`` status on agent_runs.

        SQLite cannot alter a CHECK constraint in place, so the table is
        rebuilt with a superset constraint that keeps every existing status
        value valid while adding ``blocked`` (execution denied by explicit
        policy before any provider call, e.g. the egress spärr). Historical
        rows are copied verbatim; no status is rewritten. The migration is
        idempotent: once the constraint already mentions ``blocked`` it
        returns without touching the table.
        """
        schema = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='agent_runs'
            """
        ).fetchone()
        if not schema or "'blocked'" in str(schema["sql"]):
            return

        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE agent_runs_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT,
                    issuer_id INTEGER REFERENCES issuers(id),
                    deal_id INTEGER,
                    agent_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    thinking_enabled INTEGER NOT NULL DEFAULT 0,
                    reasoning_effort TEXT,
                    prompt_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL DEFAULT 'v1',
                    execution_profile_version TEXT NOT NULL DEFAULT 'v1',
                    execution_profile_json TEXT NOT NULL DEFAULT '{}',
                    input_hash TEXT NOT NULL,
                    source_data_cutoff TEXT,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued','running','completed','failed','skipped_idempotent','unavailable','blocked')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    latency_ms INTEGER,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    estimated_cost_usd REAL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    provider_request_id TEXT,
                    confidence REAL,
                    artifact_id INTEGER REFERENCES agent_artifacts(id),
                    error_type TEXT,
                    safe_error_message TEXT,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(idempotency_key)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_runs_new
                SELECT
                    id, case_id, issuer_id, deal_id, agent_type, provider,
                    model, thinking_enabled, reasoning_effort, prompt_version,
                    schema_version, execution_profile_version,
                    execution_profile_json, input_hash, source_data_cutoff,
                    status, started_at, completed_at, latency_ms,
                    input_tokens, cached_input_tokens, output_tokens,
                    estimated_cost_usd, retry_count, provider_request_id,
                    confidence, artifact_id, error_type, safe_error_message,
                    idempotency_key, created_at
                FROM agent_runs
                """
            )
            connection.execute("DROP TABLE agent_runs")
            connection.execute(
                "ALTER TABLE agent_runs_new RENAME TO agent_runs"
            )
            connection.execute(
                """
                CREATE INDEX idx_agent_runs_issuer
                ON agent_runs(issuer_id, agent_type, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_agent_runs_status
                ON agent_runs(status, started_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_agent_runs_case
                ON agent_runs(case_id, agent_type)
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(
                    version, name, applied_at
                ) VALUES (7, 'agent_runs_blocked_status', ?)
                """,
                (now,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def start_run(self, source_id: str, run_type: str, source_url: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO source_runs (
                    source_id, run_type, source_url, started_at, status
                ) VALUES (?, ?, ?, ?, 'running')
                """,
                (source_id, run_type, source_url, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        http_status: int | None = None,
        items_seen: int = 0,
        items_upserted: int = 0,
        items_matched: int = 0,
        items_queued: int = 0,
        response_sha256: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE source_runs
                SET completed_at=?, status=?, http_status=?, items_seen=?,
                    items_upserted=?, items_matched=?, items_queued=?,
                    response_sha256=?, error=?
                WHERE id=?
                """,
                (
                    utc_now(),
                    status,
                    http_status,
                    items_seen,
                    items_upserted,
                    items_matched,
                    items_queued,
                    response_sha256,
                    error,
                    run_id,
                ),
            )

    def upsert_security(self, item: SecurityCandidate, run_id: int) -> int:
        now = utc_now()
        with self.connect() as connection:
            issuer_id = self._resolve_or_create_issuer(
                connection,
                item.issuer_name,
                item.source_id,
                item.jurisdiction_code,
            )
            connection.execute(
                """
                INSERT INTO securities (
                    issuer_id, source_id, external_id, isin, symbol, display_name,
                    currency, market, segment, mic, jurisdiction_code, active,
                    first_seen_at, last_seen_at, last_seen_run_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(source_id, external_id) DO UPDATE SET
                    issuer_id=excluded.issuer_id,
                    isin=excluded.isin,
                    symbol=excluded.symbol,
                    display_name=excluded.display_name,
                    currency=excluded.currency,
                    market=excluded.market,
                    segment=excluded.segment,
                    mic=excluded.mic,
                    jurisdiction_code=excluded.jurisdiction_code,
                    active=1,
                    last_seen_at=excluded.last_seen_at,
                    last_seen_run_id=excluded.last_seen_run_id,
                    metadata_json=excluded.metadata_json
                """,
                (
                    issuer_id,
                    item.source_id,
                    item.external_id,
                    item.isin,
                    item.symbol,
                    item.display_name,
                    item.currency,
                    item.market,
                    item.segment,
                    item.mic,
                    item.jurisdiction_code,
                    now,
                    now,
                    run_id,
                    json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            row = connection.execute(
                "SELECT id FROM securities WHERE source_id=? AND external_id=?",
                (item.source_id, item.external_id),
            ).fetchone()
            return int(row["id"])

    def mark_source_securities_inactive_except_run(
        self, source_id: str, run_ids: list[int]
    ) -> int:
        if not run_ids:
            return 0
        placeholders = ",".join("?" for _ in run_ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE securities
                SET active=0
                WHERE source_id=? AND active=1
                  AND (last_seen_run_id IS NULL OR last_seen_run_id NOT IN ({placeholders}))
                """,
                (source_id, *run_ids),
            )
            return int(cursor.rowcount)

    def create_universe_snapshot(self, source_run_ids: list[int]) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, issuer_id, source_id, external_id, isin, symbol,
                       market, segment, last_seen_run_id
                FROM securities
                WHERE active=1
                ORDER BY source_id, external_id
                """
            ).fetchall()
            checksum = stable_hash(
                *[
                    "|".join(
                        str(row[key])
                        for key in (
                            "issuer_id",
                            "source_id",
                            "external_id",
                            "isin",
                            "symbol",
                            "market",
                            "segment",
                        )
                    )
                    for row in rows
                ]
            )
            issuer_count = len({int(row["issuer_id"]) for row in rows})
            effective_run_ids = sorted(
                {
                    int(row["last_seen_run_id"])
                    for row in rows
                    if row["last_seen_run_id"] is not None
                }
                | set(source_run_ids)
            )
            cursor = connection.execute(
                """
                INSERT INTO universe_snapshots (
                    captured_at, total_issuers, total_securities, checksum,
                    source_run_ids_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    issuer_count,
                    len(rows),
                    checksum,
                    json.dumps(effective_run_ids),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO universe_snapshot_members(snapshot_id, security_id)
                VALUES (?, ?)
                """,
                ((snapshot_id, int(row["id"])) for row in rows),
            )
            return snapshot_id

    def ensure_active_universe_watchlist(
        self, *, reactivate_existing: bool = False
    ) -> int:
        """Add every active issuer to monitoring without triggering AI analysis."""
        now = utc_now()
        reactivate = int(reactivate_existing)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO issuer_watchlist (
                    issuer_id, active, added_at, updated_at
                )
                SELECT DISTINCT issuer_id, 1, ?, ?
                FROM securities
                WHERE active=1
                ON CONFLICT(issuer_id) DO UPDATE SET
                    active=CASE
                        WHEN ?=1 THEN 1
                        ELSE issuer_watchlist.active
                    END,
                    updated_at=CASE
                        WHEN ?=1 AND issuer_watchlist.active=0
                            THEN excluded.updated_at
                        ELSE issuer_watchlist.updated_at
                    END
                """,
                (now, now, reactivate, reactivate),
            )
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM issuer_watchlist w
                    WHERE w.active=1
                      AND EXISTS (
                          SELECT 1 FROM securities s
                          WHERE s.issuer_id=w.issuer_id AND s.active=1
                      )
                    """
                ).fetchone()[0]
            )

    def upsert_document(
        self, item: DocumentCandidate, *, universe_only: bool = True
    ) -> tuple[str, bool]:
        now = utc_now()
        with self.connect() as connection:
            event_issuer_name = item.issuer_name
            issuer_id = self._find_issuer(connection, item.issuer_name)
            if issuer_id is None and item.source_id == "mfn":
                issuer_id = known_mfn_issuer(connection, item.issuer_name)
                if issuer_id is not None:
                    # Global MFN and per-issuer refresh use different names.
                    # Keep one event identity without changing the source item.
                    event_issuer_name = connection.execute(
                        "SELECT canonical_name FROM issuers WHERE id=?", (issuer_id,)
                    ).fetchone()["canonical_name"]
            if issuer_id is None:
                if universe_only:
                    return "unmatched", False
                issuer_id = self._resolve_or_create_issuer(
                    connection, item.issuer_name, item.source_id, None
                )

            event_key = document_event_key(
                event_issuer_name, item.title, item.published_at, item.language
            )
            relevance = classify_title(item.title, item.category)
            existing = connection.execute(
                "SELECT id FROM documents WHERE event_key=?", (event_key,)
            ).fetchone()
            created = existing is None
            if created:
                cursor = connection.execute(
                    """
                    INSERT INTO documents (
                        issuer_id, event_key, canonical_url, title, published_at,
                        language, category, profile_category, cash_relevance_score,
                        relevance_reason, queued_for_analysis, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issuer_id,
                        event_key,
                        canonicalize_url(item.canonical_url),
                        item.title.strip(),
                        item.published_at,
                        item.language,
                        item.category,
                        relevance.profile_category,
                        relevance.score,
                        relevance.reason,
                        int(relevance.queued_for_analysis),
                        now,
                        now,
                    ),
                )
                document_id = int(cursor.lastrowid)
            else:
                document_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE documents
                    SET cash_relevance_score=MAX(cash_relevance_score, ?),
                        queued_for_analysis=MAX(queued_for_analysis, ?),
                        profile_category=CASE
                            WHEN profile_category='other'
                                THEN ?
                            ELSE profile_category
                        END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        relevance.score,
                        int(relevance.queued_for_analysis),
                        relevance.profile_category,
                        now,
                        document_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO document_sources (
                    document_id, source_id, source_external_id, discovered_url,
                    source_published_at, first_seen_at, last_seen_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_external_id) DO UPDATE SET
                    document_id=excluded.document_id,
                    discovered_url=excluded.discovered_url,
                    source_published_at=excluded.source_published_at,
                    last_seen_at=excluded.last_seen_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    document_id,
                    item.source_id,
                    item.external_id,
                    canonicalize_url(item.canonical_url),
                    item.published_at,
                    now,
                    now,
                    json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            for attachment in item.attachments:
                connection.execute(
                    """
                    INSERT INTO attachments (
                        document_id, url, file_name, mime_type
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(document_id, url) DO UPDATE SET
                        file_name=COALESCE(excluded.file_name, file_name),
                        mime_type=COALESCE(excluded.mime_type, mime_type)
                    """,
                    (
                        document_id,
                        canonicalize_url(attachment.url),
                        attachment.file_name,
                        attachment.mime_type,
                    ),
                )
            return ("created" if created else "linked"), relevance.queued_for_analysis

    def status(self) -> dict:
        with self.connect() as connection:
            latest_snapshot = connection.execute(
                """
                SELECT captured_at, total_issuers, total_securities, checksum
                FROM universe_snapshots
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            by_segment = connection.execute(
                """
                SELECT market, segment, COUNT(*) AS securities,
                       COUNT(DISTINCT issuer_id) AS issuers
                FROM securities WHERE active=1
                GROUP BY market, segment
                ORDER BY market, segment
                """
            ).fetchall()
            documents = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(queued_for_analysis), 0) AS queued,
                       COALESCE(SUM(
                           CASE WHEN analysis_status='metadata_only' THEN 1 ELSE 0 END
                       ), 0)
                           AS metadata_only
                FROM documents
                """
            ).fetchone()
            sources = connection.execute(
                """
                SELECT s.id, s.access_status, s.last_verified_at,
                       r.status AS last_run_status, r.completed_at
                FROM sources s
                LEFT JOIN source_runs r ON r.id = (
                    SELECT id FROM source_runs
                    WHERE source_id=s.id ORDER BY id DESC LIMIT 1
                )
                ORDER BY s.id
                """
            ).fetchall()
            return {
                "database": str(self.path),
                "latest_snapshot": dict(latest_snapshot) if latest_snapshot else None,
                "segments": [dict(row) for row in by_segment],
                "documents": dict(documents) if documents else {},
                "sources": [dict(row) for row in sources],
            }

    def reclassify_metadata_documents(self) -> dict[str, int]:
        updated = queued = 0
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, category
                FROM documents
                """
            ).fetchall()
            for row in rows:
                relevance = classify_title(row["title"], row["category"])
                connection.execute(
                    """
                    UPDATE documents
                    SET cash_relevance_score=?, relevance_reason=?,
                        queued_for_analysis=?, profile_category=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        relevance.score,
                        relevance.reason,
                        int(relevance.queued_for_analysis),
                        relevance.profile_category,
                        utc_now(),
                        row["id"],
                    ),
                )
                updated += 1
                queued += int(relevance.queued_for_analysis)
        return {"updated": updated, "queued": queued}

    @staticmethod
    def _find_issuer(
        connection: sqlite3.Connection, issuer_name: str
    ) -> int | None:
        normalized = normalize_issuer_name(issuer_name)
        row = connection.execute(
            "SELECT id FROM issuers WHERE normalized_name=?", (normalized,)
        ).fetchone()
        if row:
            return int(row["id"])
        row = connection.execute(
            """
            SELECT issuer_id FROM issuer_aliases
            WHERE normalized_alias=?
            ORDER BY id LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        return int(row["issuer_id"]) if row else None

    def _resolve_or_create_issuer(
        self,
        connection: sqlite3.Connection,
        issuer_name: str,
        source_id: str,
        jurisdiction_code: str | None,
    ) -> int:
        normalized = normalize_issuer_name(issuer_name)
        if not normalized:
            raise ValueError("Tomt normaliserat bolagsnamn")
        issuer_id = self._find_issuer(connection, issuer_name)
        now = utc_now()
        if issuer_id is None:
            cursor = connection.execute(
                """
                INSERT INTO issuers (
                    canonical_name, normalized_name, jurisdiction_code,
                    identity_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'provisional', ?, ?)
                """,
                (
                    issuer_display_name(issuer_name),
                    normalized,
                    jurisdiction_code,
                    now,
                    now,
                ),
            )
            issuer_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO issuer_analysis_summary (
                    issuer_id, analysis_status, capital_need_decision,
                    updated_at
                ) VALUES (?, 'not_analyzed', 'unknown', ?)
                """,
                (issuer_id, now),
            )
        connection.execute(
            """
            INSERT INTO issuer_aliases (
                issuer_id, source_id, alias, normalized_alias, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, normalized_alias) DO UPDATE SET
                issuer_id=excluded.issuer_id,
                alias=excluded.alias
            """,
            (issuer_id, source_id, issuer_name.strip(), normalized, now),
        )
        return issuer_id
