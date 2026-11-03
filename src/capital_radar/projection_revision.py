"""Persistent, constant-cost revisions for dashboard projection inputs.

Only metadata and triggers are added to the source database. The triggers
change the revision atomically with source writes; no original row is changed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid


REVISION_TABLE = "dashboard_projection_revision"
TRACKER_VERSION = "dashboard-projection-inputs-v2-change-diagnostics"
# None means every column is an input. Financial event/dossier schemas evolve;
# tracking the entire row avoids silently missing a newly added financial field.
INPUT_FIELDS = {
    "issuers": ("id", "canonical_name"),
    "documents": ("id", "issuer_id", "title", "published_at", "language", "canonical_url", "profile_category"),
    "document_sources": ("id", "document_id", "source_id", "discovered_url", "source_published_at"),
    "document_contents": ("id", "document_id", "attachment_id", "quality_score", "status", "extracted_text"),
    "document_evidence_texts": ("id", "document_id", "full_text"),
    "report_extractions": ("document_id", "is_financial_report"),
    "financing_events": None,
    "financing_event_evidence": None,
    "financing_event_participants": None,
    "financing_event_guarantee_tranches": None,
    "financing_overrides": None,
    "financing_transaction_document_overrides": None,
    "financing_transaction_projection_bindings": None,
    "financing_transaction_document_links": None,
    "financing_transaction_instrument_bindings": None,
    "financing_transaction_grouping_consolidations": None,
    "financing_transaction_stage_evidence": None,
    "financing_transaction_value_overrides": None,
    "runway_assessments": None,
    "radar_financing_weights": ("id", "transaction_id", "weight"),
    "issuer_runway_snapshots": ("issuer_id", "snapshot_date", "runway_band"),
    "financing_notification_members": None,
}


class ProjectionRevisionUnavailable(sqlite3.OperationalError):
    """Missing tracking cannot be replaced by an expensive unsafe full scan."""


def read_projection_input_changes(connection: sqlite3.Connection) -> dict:
    """Bounded metadata only; never read row IDs, source text or private terms.

    These counters begin at installation. They cannot reconstruct which input
    caused an older revision or establish why a historical build timed out.
    """
    try:
        rows = connection.execute("""SELECT input_table,change_count,last_revision,
            operation,changed_at FROM dashboard_projection_input_changes
            ORDER BY input_table LIMIT ?""", (len(INPUT_FIELDS) + 1,)).fetchall()
    except sqlite3.Error:
        return {"status": "unavailable", "coverage": "since_installation", "changes": []}
    changes = []
    for table, count, revision, operation, changed_at in rows:
        if (table not in INPUT_FIELDS or operation not in ("INSERT", "UPDATE", "DELETE")
                or type(count) is not int or count < 1
                or type(revision) is not int or revision < 1):
            return {"status": "unavailable", "coverage": "since_installation", "changes": []}
        # SQLite creates this fixed-format timestamp. Do not return arbitrary
        # strings if diagnostic metadata is unexpectedly corrupted.
        if not isinstance(changed_at, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", changed_at):
            return {"status": "unavailable", "coverage": "since_installation", "changes": []}
        changes.append({"table": table, "cumulative_change_count": count,
                        "last_revision": revision, "operation": operation,
                        "changed_at": changed_at})
    return {"status": "available", "coverage": "since_installation",
            "changes": sorted(changes, key=lambda row: row["table"])}


def diagnostics_only_successor(connection: sqlite3.Connection, previous, current) -> bool:
    """Prove the single v1→v2 metadata installation, not a financial commit.

    The installer advances once when only the tracker label changes. Exact
    old/current input-column signatures and an empty atomic change history are
    required. A source write before or after installation defeats this proof.
    """
    if (not isinstance(previous, list) or len(previous) != 3
            or not isinstance(current, list) or len(current) != 3
            or previous[0] != current[0]
            or type(previous[1]) is not int or type(current[1]) is not int
            or current[1] != previous[1] + 1):
        return False
    diagnostic = read_projection_input_changes(connection)
    if diagnostic['status'] != 'available' or diagnostic['changes']:
        return False
    fields = {}
    for table, requested in INPUT_FIELDS.items():
        columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
        fields[table] = columns if requested is None else requested
        if not columns or not set(fields[table]).issubset(columns):
            return False
    def signature(version):
        return hashlib.sha256(json.dumps([version, fields], sort_keys=True).encode()).hexdigest()
    return (previous[2] == signature('dashboard-projection-inputs-v1')
            and current[2] == signature(TRACKER_VERSION))


def read_projection_revision(connection: sqlite3.Connection) -> tuple[str, int, str]:
    try:
        row = connection.execute(
            "SELECT store_id,revision,tracker_signature FROM dashboard_projection_revision WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error as error:
        raise ProjectionRevisionUnavailable("projection_revision_unavailable") from error
    if row is None or not row[0] or type(row[1]) is not int or row[1] < 0 or not row[2]:
        raise ProjectionRevisionUnavailable("projection_revision_unavailable")
    return str(row[0]), row[1], str(row[2])


def ensure_projection_revision_schema(connection: sqlite3.Connection, *, applied_at: str) -> None:
    """Install after source backfills, without scanning rows or source text.

    Trigger changes and revision advancement are one transaction. A versioned
    signature invalidates existing projections if the dependency schema changes.
    """
    connection.execute("SAVEPOINT dashboard_projection_revision_install")
    try:
        _install_projection_revision_schema(connection, applied_at=applied_at)
        connection.execute("RELEASE SAVEPOINT dashboard_projection_revision_install")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT dashboard_projection_revision_install")
        connection.execute("RELEASE SAVEPOINT dashboard_projection_revision_install")
        raise


def _install_projection_revision_schema(connection: sqlite3.Connection, *, applied_at: str) -> None:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fields = {}
    for table, requested in INPUT_FIELDS.items():
        if table not in tables:
            raise ProjectionRevisionUnavailable("projection_input_table_unavailable")
        columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))
        fields[table] = columns if requested is None else requested
        if not set(fields[table]).issubset(columns):
            raise ProjectionRevisionUnavailable("projection_input_column_unavailable")
    signature = hashlib.sha256(json.dumps([TRACKER_VERSION, fields], sort_keys=True).encode()).hexdigest()
    connection.execute("""CREATE TABLE IF NOT EXISTS dashboard_projection_revision (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        store_id TEXT NOT NULL, tracker_signature TEXT NOT NULL)""")
    created_revision = connection.execute("INSERT OR IGNORE INTO dashboard_projection_revision VALUES(1,0,?,?)", (uuid.uuid4().hex, signature)).rowcount == 1
    connection.execute("""CREATE TABLE IF NOT EXISTS dashboard_projection_input_changes (
        input_table TEXT PRIMARY KEY, change_count INTEGER NOT NULL,
        last_revision INTEGER NOT NULL, operation TEXT NOT NULL,
        changed_at TEXT NOT NULL)""")
    if created_revision:
        # A recreated store has a new identity/revision sequence. Its old
        # diagnostic counters cannot describe changes in the new sequence.
        connection.execute("DELETE FROM dashboard_projection_input_changes")
    row = read_projection_revision(connection)
    expected = {f"trg_dashboard_projection_{table}_{operation.lower()}" for table in fields
                for operation in ("INSERT", "UPDATE", "DELETE")}
    installed = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if row[2] != signature or not expected.issubset(installed):
        for table, columns in fields.items():
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = f"trg_dashboard_projection_{table}_{operation.lower()}"
                connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                changed = ""
                update_fields = ""
                if operation == "UPDATE":
                    update_fields = " OF " + ",".join(f'"{column}"' for column in columns)
                    changed = "WHEN " + " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
                connection.execute(f'''CREATE TRIGGER "{name}" AFTER {operation}{update_fields} ON "{table}"
                    {changed} BEGIN
                    UPDATE dashboard_projection_revision SET revision=revision+1 WHERE singleton=1;
                    INSERT INTO dashboard_projection_input_changes
                        (input_table,change_count,last_revision,operation,changed_at)
                    VALUES ('{table}',1,(SELECT revision FROM dashboard_projection_revision
                        WHERE singleton=1),'{operation}',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    ON CONFLICT(input_table) DO UPDATE SET
                        change_count=change_count+1,last_revision=excluded.last_revision,
                        operation=excluded.operation,changed_at=excluded.changed_at;
                    END''')
        connection.execute("UPDATE dashboard_projection_revision SET revision=revision+1,tracker_signature=? WHERE singleton=1", (signature,))
    connection.execute("INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(29,'dashboard_projection_revisions',?)", (applied_at,))
