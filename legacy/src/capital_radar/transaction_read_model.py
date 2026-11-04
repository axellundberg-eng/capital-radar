"""Indexed dashboard projection beside source SQLite on the same volume.

Background refresh publishes one consistent source snapshot. HTTP never builds
the graph. Correction validation/readback continues using the source database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .transaction_feed import CATEGORY_LABELS, STATUS_LABELS, _build_transaction_items
from .radar_analysis import VERSION as RADAR_VERSION, current_analysis, market_health
from .projection_revision import (
    read_projection_revision, read_projection_input_changes, diagnostics_only_successor,
)

LOGGER = logging.getLogger(__name__)
# Reviewed singular recipients change financial membership, so no older payload
# can be promoted to this version through a metadata-only upgrade.
PROJECTION_VERSION = "dashboard-read-model-v22-reviewed-warrant-compensation:" + RADAR_VERSION
# v20 changed observability only. Its financial payload is byte-compatible with
# v19 using this exact radar version; older cash/model algorithms are excluded.
DIAGNOSTIC_PREDECESSOR = "dashboard-read-model-v19-reviewed-unknown-cash:" + RADAR_VERSION
DIAGNOSTIC_SUCCESSOR = "dashboard-read-model-v20-input-change-diagnostics:" + RADAR_VERSION
SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_meta(id INTEGER PRIMARY KEY CHECK(id=1),
 version TEXT NOT NULL, fingerprint TEXT NOT NULL, generation TEXT NOT NULL,
 generated_at TEXT NOT NULL, market_health_json TEXT NOT NULL,
 source_stamp_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS projection_transactions(
 id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, issuer_id INTEGER,
 category TEXT, status TEXT, published_date TEXT, search_text TEXT,
 advisors TEXT, agents TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS projection_order ON projection_transactions(ordinal);
CREATE INDEX IF NOT EXISTS projection_category_order ON projection_transactions(category,ordinal);
CREATE INDEX IF NOT EXISTS projection_status_order ON projection_transactions(status,ordinal);
CREATE INDEX IF NOT EXISTS projection_date ON projection_transactions(published_date);
CREATE TABLE IF NOT EXISTS projection_notifications(
 id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, issuer_id INTEGER,
 category TEXT, status TEXT, published_date TEXT, search_text TEXT,
 advisors TEXT, agents TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notification_order ON projection_notifications(ordinal);
CREATE INDEX IF NOT EXISTS notification_category_order ON projection_notifications(category,ordinal);
CREATE INDEX IF NOT EXISTS notification_status_order ON projection_notifications(status,ordinal);
CREATE TABLE IF NOT EXISTS projection_analyses(issuer_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projection_metadata_upgrades(
 id INTEGER PRIMARY KEY,from_fingerprint TEXT NOT NULL,to_fingerprint TEXT NOT NULL,
 from_revision_json TEXT NOT NULL,to_revision_json TEXT NOT NULL,
 reason TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(from_fingerprint,to_fingerprint));
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class TransactionReadModel:
    def __init__(self, database_path: Path, *, interval_seconds: float = 15.0,
                 refresh_timeout: float = 55.0) -> None:
        self.database_path = Path(database_path).resolve()
        self.path = self.database_path.with_name(self.database_path.name + ".read-model.sqlite3")
        self.interval_seconds = max(0.1, min(interval_seconds, 30.0))
        self.refresh_timeout = max(0.1, min(refresh_timeout, 900.0))
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._refreshing, self._stale = False, True
        self._error_code: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="transaction-read-model")
        with closing(sqlite3.connect(self.path, timeout=0.5)) as cache:
            cache.execute("PRAGMA journal_mode=WAL")
            cache.executescript(SCHEMA)
            if "source_stamp_json" not in {row[1] for row in cache.execute("PRAGMA table_info(projection_meta)")}:
                cache.execute("ALTER TABLE projection_meta ADD COLUMN source_stamp_json TEXT NOT NULL DEFAULT '[]'")
            cache.commit()

    def _source(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _reader(self, *, deadline: float | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        connection.row_factory = sqlite3.Row
        # HTTP reads retain their1s budget; background capture supplies its
        # existing whole-operation deadline instead of inheriting that HTTP cap.
        deadline = time.monotonic() + 1.0 if deadline is None else deadline
        connection.set_progress_handler(lambda: int(self._stop.is_set() or time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        return connection

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _state(self, *, refreshing: bool, stale: bool, error_code: str | None = None) -> None:
        with self._state_lock:
            self._refreshing, self._stale, self._error_code = refreshing, stale, error_code

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM projection_meta WHERE id=1").fetchone()
        if row is not None and row["version"] not in (PROJECTION_VERSION, DIAGNOSTIC_PREDECESSOR):
            row = None
        with self._state_lock:
            refreshing, stale, error = self._refreshing, self._stale, self._error_code
        published_revision = json.loads(row['source_stamp_json']) if row else None
        current_revision = None
        input_changes = {"status": "unavailable", "coverage": "since_installation", "changes": []}
        # A single metadata row catches relevant commits immediately, including
        # apply/revert and text edits without updated_at/text_sha256 changes.
        # Operational queue writes and WAL checkpoints do not invalidate data.
        if row is not None:
            try:
                with closing(self._source()) as source:
                    source.execute("BEGIN")
                    stale = (stale or row['version'] != PROJECTION_VERSION
                             or row["fingerprint"] != self._fingerprint(source, lambda: None))
                    current_revision = list(read_projection_revision(source))
                    input_changes = read_projection_input_changes(source)
            except sqlite3.Error:
                stale, error = True, "source_unavailable"
        status = ("failed" if error else "refreshing" if refreshing else
                  "pending" if row is None else "stale" if stale else "ready")
        result = {"status": status, "stale": row is not None and stale,
                  "refreshing": refreshing, "generated_at": row["generated_at"] if row else None,
                  "generation": row["generation"] if row else None, "retry_after": 2}
        comparable = (published_revision and current_revision
                      and published_revision[0] == current_revision[0]
                      and published_revision[2] == current_revision[2])
        result['revision_diagnostics'] = {
            'published_source_revision': published_revision,
            'current_source_revision': current_revision,
            'input_tracking_status': input_changes['status'],
            'input_tracking_coverage': input_changes['coverage'],
            'unpublished_inputs': [change for change in input_changes['changes']
                if comparable and change['last_revision'] > published_revision[1]],
        }
        if error:
            result["error_code"] = error
        return result

    def _fingerprint(self, source: sqlite3.Connection, checkpoint) -> str:
        """O(1) identity, read inside the same SQLite snapshot as the graph."""
        checkpoint()
        revision = read_projection_revision(source)
        checkpoint()
        return hashlib.sha256(_json([PROJECTION_VERSION, revision,
            datetime.now(timezone.utc).date().isoformat()]).encode()).hexdigest()

    def refresh_once(self) -> bool:
        """Build and atomically publish, with no AI or source writes."""
        started = time.monotonic()
        cpu_started = time.thread_time()
        deadline = started + self.refresh_timeout
        def checkpoint() -> None:
            if self._stop.is_set() or time.monotonic() > deadline:
                raise TimeoutError("projection_refresh_deadline")
        self._state(refreshing=True, stale=True)
        phase = "source_open"
        try:
            with closing(self._source()) as source:
                source.set_progress_handler(lambda: int(self._stop.is_set() or time.monotonic() > deadline), 1000)
                source.execute("BEGIN")
                phase = "revision"
                fingerprint = self._fingerprint(source, checkpoint)
                source_revision_json = _json(list(read_projection_revision(source)))
                fingerprint_ms = round((time.monotonic() - started) * 1000)
                with closing(self._reader()) as cache:
                    existing = cache.execute("SELECT fingerprint,version,source_stamp_json FROM projection_meta WHERE id=1").fetchone()
                if existing and existing["fingerprint"] == fingerprint and existing["version"] == PROJECTION_VERSION:
                    self._state(refreshing=False, stale=False)
                    return False
                if (PROJECTION_VERSION == DIAGNOSTIC_SUCCESSOR
                        and existing and existing['version'] == DIAGNOSTIC_PREDECESSOR):
                    previous = json.loads(existing['source_stamp_json'])
                    current = json.loads(source_revision_json)
                    predecessor_fingerprint = hashlib.sha256(_json([
                        DIAGNOSTIC_PREDECESSOR, previous,
                        datetime.now(timezone.utc).date().isoformat()]).encode()).hexdigest()
                    if (existing['fingerprint'] == predecessor_fingerprint
                            and diagnostics_only_successor(source, previous, current)):
                        phase = 'metadata_upgrade'
                        checkpoint()
                        with closing(sqlite3.connect(self.path, timeout=0.5)) as cache:
                            cache.execute('BEGIN IMMEDIATE')
                            changed = cache.execute('''UPDATE projection_meta SET version=?,
                                fingerprint=?,generation=?,generated_at=?,source_stamp_json=?
                                WHERE id=1 AND version=? AND fingerprint=? AND source_stamp_json=?''',
                                (PROJECTION_VERSION,fingerprint,uuid.uuid4().hex,
                                 datetime.now(timezone.utc).isoformat(),source_revision_json,
                                 existing['version'],existing['fingerprint'],existing['source_stamp_json'])).rowcount
                            if changed != 1:
                                raise sqlite3.OperationalError('projection_metadata_upgrade_conflict')
                            cache.execute('''INSERT INTO projection_metadata_upgrades
                                (from_fingerprint,to_fingerprint,from_revision_json,to_revision_json,reason,created_at)
                                VALUES(?,?,?,?,?,?)''', (existing['fingerprint'],fingerprint,
                                existing['source_stamp_json'],source_revision_json,
                                'diagnostics_only_tracker_upgrade',datetime.now(timezone.utc).isoformat()))
                            checkpoint()
                            cache.commit()
                        self._state(refreshing=False, stale=False)
                        LOGGER.info('read_model_metadata_upgraded reason=diagnostics_only_tracker_upgrade source_revision=%s previous_revision=%s',
                                    current[1],previous[1])
                        return True
                previous_revision = json.loads(existing['source_stamp_json']) if existing else None
                source_revision = json.loads(source_revision_json)
                diagnostics = read_projection_input_changes(source)
                comparable = (previous_revision and previous_revision[0] == source_revision[0]
                              and previous_revision[2] == source_revision[2])
                changed_inputs = [change for change in diagnostics['changes']
                    if comparable and change['last_revision'] > previous_revision[1]]
                LOGGER.info('read_model_refresh_inputs source_revision=%s published_revision=%s tracking_status=%s inputs=%s',
                            source_revision[1], previous_revision[1] if previous_revision else None,
                            diagnostics['status'], _json(changed_inputs))
                phase = "graph"
                graph_started = time.monotonic()
                items = _build_transaction_items(source, check_cancelled=checkpoint)
                graph_ms = round((time.monotonic() - graph_started) * 1000)
                checkpoint()
                phase = "radar"
                radar_started = time.monotonic()
                analyses = current_analysis(source, transactions=items, check_cancelled=checkpoint)
                health = market_health(source, transactions=items)
                radar_ms = round((time.monotonic() - radar_started) * 1000)
                checkpoint()
                crm = {int(row[0]): str(row[1] or "missing") for row in source.execute("SELECT issuer_id,status FROM issuer_crm")}
                rows = []
                for ordinal, original in enumerate(items):
                    checkpoint()
                    item = {**original, "crm_status": crm.get(original.get("issuer_id"), "missing")}
                    institutions = item.get("institutions", [])
                    rows.append((item["id"], ordinal, item.get("issuer_id"), item.get("category"),
                                 item.get("status"), str(item.get("published_at") or "")[:10],
                                 " ".join(str(item.get(k) or "") for k in ("issuer_name", "headline", "summary")).casefold(),
                                 "\n".join(str(p["name"]).casefold() for p in institutions if p["role"] == "Finansiell rådgivare"),
                                 "\n".join(str(p["name"]).casefold() for p in institutions if p["role"] == "Emissionsinstitut"), _json(item)))
                analysis_rows = [(item["issuer_id"], _json(item)) for item in analyses]
                health_json = _json(health)
                from .notification_groups import notifications
                notification_rows = []
                for ordinal,item in enumerate(notifications(source,[json.loads(r[-1]) for r in rows])):
                    checkpoint()
                    members = [item,*item.get('related_transactions',[])]
                    institutions = [p for m in members for p in m.get('institutions',[])]
                    notification_rows.append((item['id'],ordinal,item.get('issuer_id'),item.get('category'),item.get('status'),
                        str(item.get('published_at') or '')[:10],
                        ' '.join(str(m.get(k) or '') for m in members for k in ('issuer_name','headline','summary')).casefold(),
                        '\n'.join(str(p['name']).casefold() for p in institutions if p['role']=='Finansiell rådgivare'),
                        '\n'.join(str(p['name']).casefold() for p in institutions if p['role']=='Emissionsinstitut'),_json(item)))
            checkpoint()
            phase = "publishing"
            with closing(sqlite3.connect(self.path, timeout=0.5)) as cache:
                cache.set_progress_handler(lambda: int(self._stop.is_set() or time.monotonic() > deadline), 1000)
                cache.execute("BEGIN IMMEDIATE")
                cache.execute("DELETE FROM projection_transactions")
                cache.executemany("INSERT INTO projection_transactions VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                cache.execute('DELETE FROM projection_notifications')
                cache.executemany('INSERT INTO projection_notifications VALUES(?,?,?,?,?,?,?,?,?,?)',notification_rows)
                cache.execute("DELETE FROM projection_analyses")
                cache.executemany("INSERT INTO projection_analyses VALUES(?,?)", analysis_rows)
                cache.execute("INSERT OR REPLACE INTO projection_meta VALUES(1,?,?,?,?,?,?)",
                              (PROJECTION_VERSION, fingerprint, uuid.uuid4().hex,
                               datetime.now(timezone.utc).isoformat(), health_json, source_revision_json))
                checkpoint()
                cache.commit()
            self._state(refreshing=False, stale=False)
            LOGGER.info("read_model_refreshed transactions=%s issuers=%s duration_ms=%s revision_ms=%s graph_ms=%s radar_ms=%s thread_cpu_ms=%s",
                        len(rows), len(analysis_rows), round((time.monotonic() - started) * 1000),
                        fingerprint_ms, graph_ms, radar_ms, round((time.thread_time() - cpu_started) * 1000))
            return True
        except Exception as error:
            code = "refresh_timeout" if isinstance(error, TimeoutError) or (isinstance(error, sqlite3.OperationalError) and "interrupt" in str(error)) else "refresh_failed"
            self._state(refreshing=False, stale=True, error_code=code)
            LOGGER.warning("read_model_refresh_failed code=%s type=%s phase=%s duration_ms=%s thread_cpu_ms=%s",
                           code, type(error).__name__, phase, round((time.monotonic() - started) * 1000),
                           round((time.thread_time() - cpu_started) * 1000))
            raise

    def _run(self) -> None:
        retries = 0
        while not self._stop.is_set():
            try:
                with closing(self._source()) as observer:
                    checked_fingerprint = None
                    while not self._stop.is_set():
                        fingerprint = self._fingerprint(observer, lambda: None)
                        if fingerprint != checked_fingerprint:
                            try:
                                self.refresh_once()
                                # Use the published snapshot's marker, not a
                                # pre-build read that could lose a racing commit.
                                with closing(self._reader()) as cache:
                                    marker = cache.execute("SELECT fingerprint FROM projection_meta WHERE id=1").fetchone()
                                    checked_fingerprint = marker[0] if marker else None
                                retries = 0
                                if self._fingerprint(observer, lambda: None) != checked_fingerprint:
                                    self._state(refreshing=False, stale=True)
                            except Exception:
                                retries += 1
                        self._stop.wait(min(30.0, self.interval_seconds * (2 ** min(retries, 4))))
            except sqlite3.Error:
                self._state(refreshing=False, stale=True, error_code="source_unavailable")
                LOGGER.warning("read_model_source_unavailable")
                retries += 1
                self._stop.wait(min(30.0, self.interval_seconds * (2 ** min(retries, 4))))

    def feed(self, *, page: int = 1, page_size: int = 10, q: str = "", category: str = "",
             stage: str = "", advisor: str = "", agent: str = "", from_date: str = "") -> dict[str, Any]:
        if type(page) is not int or not 1 <= page <= 100_000 or page_size != 10:
            raise ValueError("invalid pagination")
        filters = (q, category, stage, advisor, agent, from_date)
        if any(not isinstance(value, str) or len(value) > 256 for value in filters):
            raise ValueError("invalid transaction filter")
        q, category, stage, advisor, agent, from_date = (value.strip() for value in filters)
        stages = {"announcement": ("announced", "proposed", "conditional"),
                  "terms": ("subscription_period", "approved"),
                  "outcome": ("preliminary_outcome", "completed"), "completed": ("registered",)}
        if category and category not in CATEGORY_LABELS or stage and stage not in stages and stage not in STATUS_LABELS:
            raise ValueError("invalid transaction filter")
        if from_date:
            if len(from_date) != 10:
                raise ValueError("invalid date")
            datetime.strptime(from_date, "%Y-%m-%d")
        predicates, args = [], []
        for field, value in (("search_text", q), ("advisors", advisor), ("agents", agent)):
            if value:
                predicates.append("instr(" + field + ",?)>0")
                args.append(value.casefold())
        if category:
            predicates.append("category=?")
            args.append(category)
        if stage:
            statuses = stages.get(stage, (stage,))
            predicates.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        if from_date:
            predicates.append("published_date>=?")
            args.append(from_date)
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with closing(self._reader()) as cache:
            metadata = self._metadata(cache)
            if metadata["generation"] is None:
                total, items, as_of = 0, [], None
            else:
                total = cache.execute("SELECT COUNT(*) FROM projection_notifications" + where, args).fetchone()[0]
                rows = cache.execute("SELECT payload FROM projection_notifications" + where + " ORDER BY ordinal LIMIT ? OFFSET ?",
                                     (*args, page_size, (page - 1) * page_size)).fetchall()
                items = [json.loads(row[0]) for row in rows]
                first = cache.execute("SELECT payload FROM projection_notifications" + where + " ORDER BY ordinal LIMIT 1", args).fetchone()
                as_of = json.loads(first[0]).get("published_at") if first else None
        return {"items": items, "page": page, "page_size": page_size, "total": total,
                "total_pages": (total + page_size - 1) // page_size,
                "has_next": page * page_size < total, "as_of": as_of, "read_model": metadata}

    def analysis(self, issuer_ids: list[int] | None = None) -> dict[str, Any]:
        with closing(self._reader()) as cache:
            metadata = self._metadata(cache)
            if metadata["generation"] is None or issuer_ids == []:
                items = []
            else:
                where = " WHERE issuer_id IN (" + ",".join("?" for _ in issuer_ids) + ")" if issuer_ids is not None else ""
                rows = cache.execute("SELECT payload FROM projection_analyses" + where + " ORDER BY issuer_id", issuer_ids or []).fetchall()
                items = [json.loads(row[0]) for row in rows]
        return {"items": items, "read_model": metadata}

    def health(self) -> dict[str, Any]:
        with closing(self._reader()) as cache:
            metadata = self._metadata(cache)
            row = cache.execute("SELECT market_health_json FROM projection_meta WHERE id=1").fetchone()
            result = json.loads(row[0]) if row and metadata["generation"] is not None else {"months": []}
        return {**result, "read_model": metadata}

    def detail(self, transaction_id: str) -> dict[str, Any]:
        if not transaction_id.startswith("transaction:") or len(transaction_id) > 180:
            raise ValueError("invalid transaction id")
        with closing(self._reader()) as cache:
            metadata = self._metadata(cache)
            if metadata["generation"] is None:
                return {"id": transaction_id, "read_model": metadata}
            row = cache.execute("SELECT payload FROM projection_transactions WHERE id=?", (transaction_id,)).fetchone()
        if row is None:
            raise KeyError(transaction_id)
        return {**json.loads(row[0]), "read_model": metadata}
