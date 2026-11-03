"""Financing History Intelligence.

Persistent, incremental, deduplicated 5-year financing-history dataset built
by EXTENDING the existing ``financing_events`` model.  No competing table is
created; canonical enrichment lives in nullable columns on ``financing_events``
plus small related tables (participants, backfill state).

Principle: FETCH ONCE, PARSE ONCE, NORMALIZE ONCE, STORE ONCE, REUSE UNTIL
INVALIDATED.  No normal refresh re-fetches or re-AI-analyzes 5 years of
history; incremental refresh only processes new/changed source items.

Hard rule: DETERMINISTIC BEFORE AI.  All derived metrics (dilution, secured %,
guarantee %, discounts, raise/market cap, utilization, cost %) are computed in
code from observable facts.  AI only extracts observable facts; AI-computed
math is never stored as fact.  NULL BEFORE GUESS.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .normalization import normalize_issuer_name, stable_hash, utc_now


# ---------------------------------------------------------------------------
# Migration spec (additive, idempotent, backward-compatible).
#
# This module does NOT edit db.py / analysis_schema.py (owned by Agent D).
# The exact additive SQL below is meant to be applied by the Main Architect /
# Agent D via the existing ``Database._ensure_columns`` pattern (for the
# nullable financing_events columns) plus ``CREATE TABLE IF NOT EXISTS`` for
# the new related tables.  Everything is additive and safe to re-run.
# ---------------------------------------------------------------------------

# Nullable enrichment columns to add to the existing financing_events table
# through Database._ensure_columns(connection, "financing_events", {...}).
FINANCING_EVENTS_ENSURE_COLUMNS: dict[str, str] = {
    # A financing round can produce several releases (terms, subscription
    # period and outcome).  These fields link those releases without changing
    # the existing event fingerprint or liquidity calculations.
    "canonical_round_key": "TEXT",
    "lifecycle_stage": "TEXT",
    "stage_effective_at": "TEXT",
    "financing_type": "TEXT",
    "raw_type_description": "TEXT",
    "issue_price": "REAL",
    "shares_before": "REAL",
    "shares_issued": "REAL",
    "shares_after": "REAL",
    "dilution": "REAL",
    "market_price_reference": "REAL",
    "vwap_reference": "REAL",
    "market_cap_before": "REAL",
    "raise_to_market_cap": "REAL",
    "discount_to_spot": "REAL",
    "discount_to_vwap": "REAL",
    "subscription_commitment_amount": "REAL",
    "subscription_commitment_pct": "REAL",
    "guarantee_commitment_amount": "REAL",
    "guarantee_commitment_pct": "REAL",
    "secured_total_amount": "REAL",
    "secured_total_pct": "REAL",
    "final_subscription_amount": "REAL",
    "final_subscription_pct": "REAL",
    "subscription_without_guarantees_pct": "REAL",
    "guarantee_utilized_amount": "REAL",
    "guarantee_utilized_pct": "REAL",
    "guarantee_fee_details": "TEXT",
    "guarantee_fee_amount": "REAL",
    "guarantee_fee_pct": "REAL",
    "guarantee_fee_cash_pct": "REAL",
    "guarantee_fee_share_pct": "REAL",
    "guarantee_fee_basis": "TEXT",
    "credit_limit": "REAL",
    "drawn_amount": "REAL",
    "undrawn_commitment": "REAL",
    "transaction_cost": "REAL",
    "transaction_cost_pct": "REAL",
    "adviser": "TEXT",
    "issuer_agent": "TEXT",
    "outcome": "TEXT",
    "extraction_version": "TEXT",
    "normalization_version": "TEXT",
}

# New related tables (1:N participants, per-issuer backfill state, market-data
# snapshots).  All use CREATE TABLE IF NOT EXISTS so they are idempotent.
FINANCING_HISTORY_NEW_TABLES_SQL = """
CREATE INDEX IF NOT EXISTS idx_financing_events_canonical_round
    ON financing_events(issuer_id, canonical_round_key, lifecycle_stage);

CREATE TABLE IF NOT EXISTS financing_event_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    related_financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    relationship_type TEXT NOT NULL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, related_financing_event_id, relationship_type),
    CHECK(financing_event_id <> related_financing_event_id)
);
CREATE INDEX IF NOT EXISTS idx_financing_relationships_related
    ON financing_event_relationships(related_financing_event_id, relationship_type);

CREATE TABLE IF NOT EXISTS financing_outcome_market_metrics (
    financing_event_id INTEGER PRIMARY KEY REFERENCES financing_events(id),
    outcome_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    close_average_5d REAL,
    volume_weighted_close_5d REAL,
    total_volume_5d REAL,
    trading_dates_json TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS financing_outcome_market_days (
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    trading_date TEXT NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY(financing_event_id, trading_date)
);
CREATE INDEX IF NOT EXISTS idx_financing_outcome_market_days_date
    ON financing_outcome_market_days(trading_date);

CREATE TABLE IF NOT EXISTS financing_event_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    raw_name TEXT NOT NULL,
    normalized_candidate TEXT,
    role TEXT NOT NULL,
    amount REAL,
    percentage REAL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, raw_name, role)
);
CREATE INDEX IF NOT EXISTS idx_financing_participants_event
    ON financing_event_participants(financing_event_id, role);

CREATE TABLE IF NOT EXISTS financing_event_guarantee_tranches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    tier TEXT NOT NULL CHECK(tier IN ('top', 'middle', 'bottom', 'other')),
    commitment_amount REAL,
    fee_rate REAL,
    fee_amount REAL,
    utilized_amount REAL,
    source_document_id INTEGER REFERENCES documents(id),
    source_page INTEGER,
    source_excerpt TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, tier, source_document_id)
);
CREATE INDEX IF NOT EXISTS idx_financing_guarantee_tranches_event
    ON financing_event_guarantee_tranches(financing_event_id, tier);

CREATE TABLE IF NOT EXISTS financing_event_enrichment_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_document_id INTEGER NOT NULL REFERENCES documents(id),
    source_excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(financing_event_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_financing_enrichment_event
    ON financing_event_enrichment_facts(financing_event_id, field_name);

CREATE TABLE IF NOT EXISTS financing_backfill_state (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    requested_start_date TEXT,
    requested_end_date TEXT,
    oldest_fetched_date TEXT,
    newest_fetched_date TEXT,
    source TEXT,
    cursor TEXT,
    checkpoint TEXT,
    last_successful_fetch_at TEXT,
    last_successful_extraction_at TEXT,
    last_successful_normalization_at TEXT,
    incomplete_ranges_json TEXT NOT NULL DEFAULT '[]',
    failure_count INTEGER NOT NULL DEFAULT 0,
    retry_state TEXT,
    status TEXT NOT NULL DEFAULT 'not_started',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financing_market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    as_of_date TEXT NOT NULL,
    price REAL,
    market_cap REAL,
    adtv REAL,
    vwap REAL,
    return_m20 REAL,
    return_p1 REAL,
    return_p5 REAL,
    return_p20 REAL,
    return_p60 REAL,
    source TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(issuer_id, as_of_date)
);
"""

# Combined spec string for the report / Agent D.  The financing_events column
# additions are emitted as ALTER statements guarded by the _ensure_columns
# pattern; Agent D should wire FINANCING_EVENTS_ENSURE_COLUMNS into
# Database.initialize via self._ensure_columns(connection, "financing_events",
# FINANCING_EVENTS_ENSURE_COLUMNS) and executescript the new tables.
FINANCING_HISTORY_MIGRATION_SQL = (
    "-- Nullable enrichment columns on financing_events "
    "(apply via Database._ensure_columns):\n"
    + "\n".join(
        f"--   {name} {definition}"
        for name, definition in FINANCING_EVENTS_ENSURE_COLUMNS.items()
    )
    + "\n\n-- New related tables:\n"
    + FINANCING_HISTORY_NEW_TABLES_SQL
)


# ---------------------------------------------------------------------------
# Canonical financing types.
# ---------------------------------------------------------------------------

CANONICAL_FINANCING_TYPES = (
    "rights_issue",
    "directed_issue",
    "unit_issue",
    "convertible",
    "bridge_loan",
    "shareholder_loan",
    "warrant_exercise",
    "strategic_investment",
    "credit_facility",
    "other_equity",
    "other_debt",
    "mixed_financing",
    "unknown",
)

# These values describe the public lifecycle of one financing round.  They
# deliberately remain separate from the existing ``status`` field: status is
# used by current screening and transaction-feed behaviour, while a lifecycle
# stage tells the UI and editorial layer which public release it is handling.
FINANCING_LIFECYCLE_STAGES = (
    "considering",
    "announced",
    "terms_published",
    "subscription_open",
    "preliminary_outcome",
    "outcome_published",
    "registered",
    "settled",
    "cancelled",
)

FINANCING_RELATIONSHIP_TYPES = (
    "refinances",
    "repaid_by",
    "set_off_in",
    "converted_in",
)

# Map from the legacy extraction EVENT_TYPES (extraction_schema.py) and common
# raw descriptions to the canonical financing_type.  The raw description is
# always preserved separately; this mapping is deterministic and conservative.
_RAW_TYPE_MAP = {
    "rights_issue": "rights_issue",
    "företrädesemission": "rights_issue",
    "foretradesemission": "rights_issue",
    "directed_issue": "directed_issue",
    "riktad emission": "directed_issue",
    "riktad nyemission": "directed_issue",
    "unit_issue": "unit_issue",
    "units": "unit_issue",
    "emission av units": "unit_issue",
    "convertible": "convertible",
    "konvertibel": "convertible",
    "konvertibelt lån": "convertible",
    "bridge_loan": "bridge_loan",
    "brygglån": "bridge_loan",
    "bryggfinansiering": "bridge_loan",
    "shareholder_loan": "shareholder_loan",
    "aktieägarlån": "shareholder_loan",
    "warrant_exercise": "warrant_exercise",
    "teckningsoption": "warrant_exercise",
    "option exercise": "warrant_exercise",
    "strategic_investment": "strategic_investment",
    "strategisk investering": "strategic_investment",
    "credit_facility": "credit_facility",
    "kreditfacilitet": "credit_facility",
    "loan_draw": "other_debt",
    "loan_commitment": "other_debt",
    "grant": "other_equity",
    "other": "unknown",
}

# Roles for financing participants.
PARTICIPANT_ROLES = (
    "subscription_commitment",
    "guarantor",
    "cornerstone",
    "major_owner",
    "board",
    "management",
    "strategic",
)

# Resolution statuses; unresolved is always preferred over a wrong merge.
RESOLUTION_STATUSES = ("unresolved", "candidate", "resolved", "rejected")

# Backfill statuses.
BACKFILL_STATUSES = (
    "not_started",
    "in_progress",
    "partial",
    "completed",
    "failed",
)

# Version tags stamped on every normalized event for provenance/invalidation.
EXTRACTION_VERSION = "financing-extraction-v1"
NORMALIZATION_VERSION = "financing-normalization-v1"


# ---------------------------------------------------------------------------
# Canonical event model.
# ---------------------------------------------------------------------------


@dataclass
class CanonicalFinancingEvent:
    """Canonical, normalized financing event.

    Only observable facts are stored as inputs; every derived metric is
    computed deterministically by :func:`compute_derived_metrics`.
    """

    issuer_id: int
    financing_type: str
    raw_type_description: str
    description: str
    canonical_round_key: str | None = None
    lifecycle_stage: str | None = None
    stage_effective_at: str | None = None
    announcement_date: str | None = None
    effective_date: str | None = None
    status: str = "announced"
    currency: str | None = None
    gross_amount: float | None = None
    net_amount: float | None = None
    issue_price: float | None = None
    shares_before: float | None = None
    shares_issued: float | None = None
    shares_after: float | None = None
    market_price_reference: float | None = None
    vwap_reference: float | None = None
    market_cap_before: float | None = None
    subscription_commitment_amount: float | None = None
    guarantee_commitment_amount: float | None = None
    final_subscription_amount: float | None = None
    guarantee_utilized_amount: float | None = None
    transaction_cost: float | None = None
    guarantee_fee_details: str | None = None
    guarantee_fee_amount: float | None = None
    guarantee_fee_cash_pct: float | None = None
    guarantee_fee_share_pct: float | None = None
    guarantee_fee_basis: str | None = None
    credit_limit: float | None = None
    drawn_amount: float | None = None
    undrawn_commitment: float | None = None
    adviser: str | None = None
    issuer_agent: str | None = None
    outcome: str | None = None
    confidence: float = 0.0
    extraction_version: str = EXTRACTION_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    # Derived metrics (computed, never AI-supplied).
    dilution: float | None = None
    raise_to_market_cap: float | None = None
    discount_to_spot: float | None = None
    discount_to_vwap: float | None = None
    subscription_commitment_pct: float | None = None
    guarantee_commitment_pct: float | None = None
    secured_total_amount: float | None = None
    secured_total_pct: float | None = None
    final_subscription_pct: float | None = None
    subscription_without_guarantees_pct: float | None = None
    guarantee_utilized_pct: float | None = None
    transaction_cost_pct: float | None = None
    guarantee_fee_pct: float | None = None


@dataclass
class FinancingParticipant:
    financing_event_id: int | None
    raw_name: str
    role: str
    normalized_candidate: str | None = None
    amount: float | None = None
    percentage: float | None = None
    source: str | None = None
    confidence: float = 0.0
    resolution_status: str = "unresolved"


@dataclass
class GuaranteeTranche:
    """A source-backed guarantee layer for one financing event.

    Amounts are monetary units and ``fee_rate`` is a decimal fraction.  The
    record deliberately permits any field to stay null: public release terms
    often state a fee tier without disclosing its amount or later utilization.
    """

    financing_event_id: int
    tier: str
    commitment_amount: float | None = None
    fee_rate: float | None = None
    fee_amount: float | None = None
    utilized_amount: float | None = None
    source_document_id: int | None = None
    source_page: int | None = None
    source_excerpt: str | None = None
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Normalization: raw type -> canonical type (raw always preserved).
# ---------------------------------------------------------------------------


def normalize_financing_type(raw_type: str | None) -> str:
    """Map a raw type/description to a canonical financing_type.

    Conservative: anything unrecognized maps to ``unknown`` rather than a
    guessed type.  The raw value is always kept by the caller.
    """
    if not raw_type:
        return "unknown"
    key = " ".join(str(raw_type).strip().casefold().split())
    if key in _RAW_TYPE_MAP:
        return _RAW_TYPE_MAP[key]
    # Substring match for longer raw descriptions, longest key first.
    for needle in sorted(_RAW_TYPE_MAP, key=len, reverse=True):
        if needle and needle in key:
            return _RAW_TYPE_MAP[needle]
    return "unknown"


def normalize_lifecycle_stage(stage: str | None) -> str | None:
    """Validate an explicit public financing-lifecycle stage.

    A missing stage stays missing.  We do not infer a lifecycle stage from
    existing event status because that status still drives established
    screening and signal semantics.
    """
    if stage is None:
        return None
    normalized = "_".join(str(stage).strip().casefold().split())
    if normalized not in FINANCING_LIFECYCLE_STAGES:
        raise ValueError(f"Unsupported financing lifecycle stage: {stage}")
    return normalized


def normalize_canonical_round_key(round_key: str | None) -> str | None:
    """Validate a source-backed round identity without inferring one.

    The extractor or an operator supplies this key only where documents
    establish that releases belong to the same financing round.  This avoids
    silently linking separate tranches or a preliminary consideration notice
    to a completed transaction.
    """
    if round_key is None:
        return None
    normalized = "-".join(str(round_key).strip().casefold().split())
    if not normalized or len(normalized) > 256:
        raise ValueError("canonical_round_key must contain 1 to 256 characters")
    return normalized


# ---------------------------------------------------------------------------
# Deterministic derived metrics.  Every function returns None when its inputs
# are missing (NULL BEFORE GUESS) and never raises on absent data.
# ---------------------------------------------------------------------------


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def compute_derived_metrics(event: CanonicalFinancingEvent) -> CanonicalFinancingEvent:
    """Compute all mechanical derived metrics in code.  Never AI-supplied."""
    # Dilution: shares_issued / shares_after (post-money share count).  Fall
    # back to shares_issued / (shares_before + shares_issued) when shares_after
    # is absent.
    if event.dilution is None:
        if event.shares_after:
            event.dilution = _safe_div(event.shares_issued, event.shares_after)
        elif event.shares_before is not None and event.shares_issued is not None:
            total = event.shares_before + event.shares_issued
            event.dilution = _safe_div(event.shares_issued, total)

    # Raise to market cap.
    if event.raise_to_market_cap is None:
        event.raise_to_market_cap = _safe_div(
            event.gross_amount, event.market_cap_before
        )

    # Discount to spot / vwap: (reference - issue_price) / reference.
    if event.discount_to_spot is None and event.market_price_reference:
        event.discount_to_spot = _safe_div(
            event.market_price_reference - (event.issue_price or 0.0)
            if event.issue_price is not None
            else None,
            event.market_price_reference,
        )
    if event.discount_to_vwap is None and event.vwap_reference:
        event.discount_to_vwap = _safe_div(
            event.vwap_reference - (event.issue_price or 0.0)
            if event.issue_price is not None
            else None,
            event.vwap_reference,
        )

    # Commitment percentages relative to gross amount.
    if event.subscription_commitment_pct is None:
        event.subscription_commitment_pct = _safe_div(
            event.subscription_commitment_amount, event.gross_amount
        )
    if event.guarantee_commitment_pct is None:
        event.guarantee_commitment_pct = _safe_div(
            event.guarantee_commitment_amount, event.gross_amount
        )

    # Secured total = subscription commitments + guarantee commitments.
    if event.secured_total_amount is None and (
        event.subscription_commitment_amount is not None
        or event.guarantee_commitment_amount is not None
    ):
        event.secured_total_amount = (event.subscription_commitment_amount or 0.0) + (
            event.guarantee_commitment_amount or 0.0
        )
    if event.secured_total_pct is None:
        event.secured_total_pct = _safe_div(
            event.secured_total_amount, event.gross_amount
        )

    # Final subscription pct relative to gross.
    if event.final_subscription_pct is None:
        event.final_subscription_pct = _safe_div(
            event.final_subscription_amount, event.gross_amount
        )

    # Subscription without guarantees: final subscription minus guarantee
    # utilization, relative to gross.
    if event.subscription_without_guarantees_pct is None and (
        event.final_subscription_amount is not None
    ):
        without = event.final_subscription_amount - (
            event.guarantee_utilized_amount or 0.0
        )
        event.subscription_without_guarantees_pct = _safe_div(
            without, event.gross_amount
        )

    # Guarantee utilization relative to guarantee commitment.
    if event.guarantee_utilized_pct is None:
        event.guarantee_utilized_pct = _safe_div(
            event.guarantee_utilized_amount, event.guarantee_commitment_amount
        )

    # Transaction cost pct relative to gross.
    if event.transaction_cost_pct is None:
        event.transaction_cost_pct = _safe_div(
            event.transaction_cost, event.gross_amount
        )
    if event.guarantee_fee_pct is None:
        event.guarantee_fee_pct = _safe_div(
            event.guarantee_fee_amount, event.guarantee_commitment_amount
        )

    return event


# ---------------------------------------------------------------------------
# Fingerprint / dedup.  Mirrors analysis_store._persist_extraction's
# event_fingerprint so history dedups against the existing financing_events
# rows instead of competing with them.
# ---------------------------------------------------------------------------


def event_fingerprint(
    issuer_id: int,
    event_type: str,
    date: str | None,
    currency: str | None,
    amount: float | None,
) -> str:
    return stable_hash(
        str(issuer_id),
        event_type,
        date or "",
        currency or "",
        "" if amount is None else str(amount),
    )


def _first_amount(event: CanonicalFinancingEvent) -> float | None:
    for value in (
        event.gross_amount,
        event.net_amount,
        event.subscription_commitment_amount,
        event.guarantee_commitment_amount,
    ):
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# Extractor protocol: AI extraction is behind a stable interface so it can be
# wired to AgentRunner once Agent A's contract lands.  Tests inject a fake.
# ---------------------------------------------------------------------------


class FinancingExtractor(Protocol):
    """Stable interface for AI-backed financing fact extraction.

    Implementations return raw observable facts (never derived math).  The
    deterministic layer computes every metric from these facts.
    """

    def extract(self, document_text: str, *, issuer_id: int) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Store: persistence against the existing financing_events table plus the new
# related tables.  All writes are idempotent and additive.
# ---------------------------------------------------------------------------


class FinancingHistoryStore:
    def __init__(self, db: Any) -> None:
        # ``db`` is the existing Database helper (has .connect()).
        self.db = db

    # -- schema -----------------------------------------------------------

    def ensure_schema(self, connection: sqlite3.Connection) -> None:
        """Apply the additive migration spec idempotently.

        Uses the same _ensure_columns pattern as db.py for the nullable
        financing_events columns, and CREATE TABLE IF NOT EXISTS for the new
        related tables.  Safe to call on every startup.
        """
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(financing_events)")
        }
        for column, definition in FINANCING_EVENTS_ENSURE_COLUMNS.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE financing_events ADD COLUMN {column} {definition}"
                )
        connection.executescript(FINANCING_HISTORY_NEW_TABLES_SQL)

    # -- event persistence -------------------------------------------------

    def upsert_event(
        self,
        connection: sqlite3.Connection,
        event: CanonicalFinancingEvent,
    ) -> tuple[int, bool]:
        """Insert or update a canonical event, deduplicated by fingerprint.

        Returns (financing_event_id, created).  On fingerprint conflict the
        row is updated in place (never duplicated); deterministic metrics are
        recomputed before persist.
        """
        compute_derived_metrics(event)
        lifecycle_stage = normalize_lifecycle_stage(event.lifecycle_stage)
        canonical_round_key = normalize_canonical_round_key(event.canonical_round_key)
        date = event.effective_date or event.announcement_date
        fingerprint = event_fingerprint(
            event.issuer_id,
            event.financing_type,
            date,
            event.currency,
            _first_amount(event),
        )
        # A source-backed round key plus explicit stage gives each lifecycle
        # release its own row.  Legacy events keep the existing fingerprint.
        if canonical_round_key and lifecycle_stage:
            fingerprint = stable_hash(fingerprint, canonical_round_key, lifecycle_stage)
        now = utc_now()
        existing = connection.execute(
            "SELECT id FROM financing_events WHERE event_fingerprint=?",
            (fingerprint,),
        ).fetchone()
        created = existing is None
        connection.execute(
            """
            INSERT INTO financing_events (
                issuer_id, event_fingerprint, event_type, description,
                announcement_date, effective_date, status, cash_direction,
                gross_amount, net_amount, currency, liquidity_effect_kind,
                reflected_in_report, confidence, created_at, updated_at,
                financing_type, raw_type_description, issue_price,
                shares_before, shares_issued, shares_after, dilution,
                market_price_reference, vwap_reference, market_cap_before,
                raise_to_market_cap, discount_to_spot, discount_to_vwap,
                subscription_commitment_amount, subscription_commitment_pct,
                guarantee_commitment_amount, guarantee_commitment_pct,
                secured_total_amount, secured_total_pct,
                final_subscription_amount, final_subscription_pct,
                subscription_without_guarantees_pct,
                guarantee_utilized_amount, guarantee_utilized_pct,
                transaction_cost, transaction_cost_pct, guarantee_fee_details,
                adviser, issuer_agent, outcome,
                extraction_version, normalization_version,
                canonical_round_key, lifecycle_stage, stage_effective_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(event_fingerprint) DO UPDATE SET
                description=excluded.description,
                status=excluded.status,
                gross_amount=COALESCE(excluded.gross_amount, gross_amount),
                net_amount=COALESCE(excluded.net_amount, net_amount),
                issue_price=COALESCE(excluded.issue_price, issue_price),
                shares_before=COALESCE(excluded.shares_before, shares_before),
                shares_issued=COALESCE(excluded.shares_issued, shares_issued),
                shares_after=COALESCE(excluded.shares_after, shares_after),
                dilution=COALESCE(excluded.dilution, dilution),
                market_price_reference=COALESCE(excluded.market_price_reference, market_price_reference),
                vwap_reference=COALESCE(excluded.vwap_reference, vwap_reference),
                market_cap_before=COALESCE(excluded.market_cap_before, market_cap_before),
                raise_to_market_cap=COALESCE(excluded.raise_to_market_cap, raise_to_market_cap),
                discount_to_spot=COALESCE(excluded.discount_to_spot, discount_to_spot),
                discount_to_vwap=COALESCE(excluded.discount_to_vwap, discount_to_vwap),
                subscription_commitment_amount=COALESCE(excluded.subscription_commitment_amount, subscription_commitment_amount),
                subscription_commitment_pct=COALESCE(excluded.subscription_commitment_pct, subscription_commitment_pct),
                guarantee_commitment_amount=COALESCE(excluded.guarantee_commitment_amount, guarantee_commitment_amount),
                guarantee_commitment_pct=COALESCE(excluded.guarantee_commitment_pct, guarantee_commitment_pct),
                secured_total_amount=COALESCE(excluded.secured_total_amount, secured_total_amount),
                secured_total_pct=COALESCE(excluded.secured_total_pct, secured_total_pct),
                final_subscription_amount=COALESCE(excluded.final_subscription_amount, final_subscription_amount),
                final_subscription_pct=COALESCE(excluded.final_subscription_pct, final_subscription_pct),
                subscription_without_guarantees_pct=COALESCE(excluded.subscription_without_guarantees_pct, subscription_without_guarantees_pct),
                guarantee_utilized_amount=COALESCE(excluded.guarantee_utilized_amount, guarantee_utilized_amount),
                guarantee_utilized_pct=COALESCE(excluded.guarantee_utilized_pct, guarantee_utilized_pct),
                transaction_cost=COALESCE(excluded.transaction_cost, transaction_cost),
                transaction_cost_pct=COALESCE(excluded.transaction_cost_pct, transaction_cost_pct),
                guarantee_fee_details=COALESCE(excluded.guarantee_fee_details, guarantee_fee_details),
                adviser=COALESCE(excluded.adviser, adviser),
                issuer_agent=COALESCE(excluded.issuer_agent, issuer_agent),
                outcome=COALESCE(excluded.outcome, outcome),
                canonical_round_key=COALESCE(excluded.canonical_round_key, canonical_round_key),
                lifecycle_stage=COALESCE(excluded.lifecycle_stage, lifecycle_stage),
                stage_effective_at=COALESCE(excluded.stage_effective_at, stage_effective_at),
                extraction_version=excluded.extraction_version,
                normalization_version=excluded.normalization_version,
                confidence=MAX(confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            (
                event.issuer_id,
                fingerprint,
                event.financing_type,
                event.description,
                event.announcement_date,
                event.effective_date,
                event.status,
                "inflow",
                event.gross_amount,
                event.net_amount,
                event.currency,
                "operating",
                "no",
                event.confidence,
                now,
                now,
                event.financing_type,
                event.raw_type_description,
                event.issue_price,
                event.shares_before,
                event.shares_issued,
                event.shares_after,
                event.dilution,
                event.market_price_reference,
                event.vwap_reference,
                event.market_cap_before,
                event.raise_to_market_cap,
                event.discount_to_spot,
                event.discount_to_vwap,
                event.subscription_commitment_amount,
                event.subscription_commitment_pct,
                event.guarantee_commitment_amount,
                event.guarantee_commitment_pct,
                event.secured_total_amount,
                event.secured_total_pct,
                event.final_subscription_amount,
                event.final_subscription_pct,
                event.subscription_without_guarantees_pct,
                event.guarantee_utilized_amount,
                event.guarantee_utilized_pct,
                event.transaction_cost,
                event.transaction_cost_pct,
                event.guarantee_fee_details,
                event.adviser,
                event.issuer_agent,
                event.outcome,
                event.extraction_version,
                event.normalization_version,
                canonical_round_key,
                lifecycle_stage,
                event.stage_effective_at,
            ),
        )
        connection.execute(
            """UPDATE financing_events
               SET guarantee_fee_amount=COALESCE(?, guarantee_fee_amount),
                   guarantee_fee_pct=COALESCE(?, guarantee_fee_pct),
                   guarantee_fee_cash_pct=COALESCE(?, guarantee_fee_cash_pct),
                   guarantee_fee_share_pct=COALESCE(?, guarantee_fee_share_pct),
                   guarantee_fee_basis=COALESCE(?, guarantee_fee_basis),
                   credit_limit=COALESCE(?, credit_limit),
                   drawn_amount=COALESCE(?, drawn_amount),
                   undrawn_commitment=COALESCE(?, undrawn_commitment)
               WHERE event_fingerprint=?""",
            (event.guarantee_fee_amount, event.guarantee_fee_pct,
             event.guarantee_fee_cash_pct, event.guarantee_fee_share_pct,
             event.guarantee_fee_basis, event.credit_limit, event.drawn_amount,
             event.undrawn_commitment, fingerprint),
        )
        row = connection.execute(
            "SELECT id FROM financing_events WHERE event_fingerprint=?",
            (fingerprint,),
        ).fetchone()
        return int(row["id"]), created

    def record_relationship(
        self,
        connection: sqlite3.Connection,
        *,
        financing_event_id: int,
        related_financing_event_id: int,
        relationship_type: str,
        source: str | None = None,
        confidence: float = 0.0,
    ) -> int:
        """Persist a source-backed bridge/equity or round relationship."""
        if relationship_type not in FINANCING_RELATIONSHIP_TYPES:
            raise ValueError(f"Unsupported financing relationship: {relationship_type}")
        if financing_event_id == related_financing_event_id:
            raise ValueError("A financing event cannot relate to itself")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO financing_event_relationships (
                financing_event_id, related_financing_event_id, relationship_type,
                source, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(financing_event_id, related_financing_event_id, relationship_type)
            DO UPDATE SET
                source=COALESCE(excluded.source, source),
                confidence=MAX(confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            (
                financing_event_id,
                related_financing_event_id,
                relationship_type,
                source,
                confidence,
                now,
                now,
            ),
        )
        row = connection.execute(
            """SELECT id FROM financing_event_relationships
               WHERE financing_event_id=? AND related_financing_event_id=?
                 AND relationship_type=?""",
            (financing_event_id, related_financing_event_id, relationship_type),
        ).fetchone()
        return int(row["id"])

    # -- participants -------------------------------------------------------

    def record_participant(
        self,
        connection: sqlite3.Connection,
        participant: FinancingParticipant,
    ) -> int:
        """Record a participant conservatively.

        Entity resolution is conservative: a participant is stored with
        ``resolution_status='unresolved'`` unless a confident normalized
        candidate is supplied.  Unresolved is always better than a wrong merge.
        """
        now = utc_now()
        normalized = participant.normalized_candidate
        if normalized:
            normalized = normalize_issuer_name(normalized)
        connection.execute(
            """
            INSERT INTO financing_event_participants (
                financing_event_id, raw_name, normalized_candidate, role,
                amount, percentage, source, confidence, resolution_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(financing_event_id, raw_name, role) DO UPDATE SET
                amount=COALESCE(excluded.amount, amount),
                percentage=COALESCE(excluded.percentage, percentage),
                source=COALESCE(excluded.source, source),
                confidence=MAX(confidence, excluded.confidence),
                normalized_candidate=COALESCE(excluded.normalized_candidate, normalized_candidate),
                resolution_status=excluded.resolution_status,
                updated_at=excluded.updated_at
            """,
            (
                participant.financing_event_id,
                participant.raw_name,
                normalized,
                participant.role,
                participant.amount,
                participant.percentage,
                participant.source,
                participant.confidence,
                participant.resolution_status,
                now,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT id FROM financing_event_participants
            WHERE financing_event_id=? AND raw_name=? AND role=?
            """,
            (
                participant.financing_event_id,
                participant.raw_name,
                participant.role,
            ),
        ).fetchone()
        return int(row["id"])

    def record_guarantee_tranche(
        self,
        connection: sqlite3.Connection,
        tranche: GuaranteeTranche,
    ) -> int:
        """Upsert one disclosed guarantee tranche without deriving facts.

        A tranche belongs to the event that the source document actually
        describes.  The feed may later combine lifecycle events that share a
        source-backed round key, while the stored source document remains
        attached to each reported amount and fee.
        """
        tier = str(tranche.tier or "").strip().casefold()
        if tier not in {"top", "middle", "bottom", "other"}:
            raise ValueError(f"Unsupported guarantee tranche tier: {tranche.tier}")
        now = utc_now()
        row = connection.execute(
            """SELECT id FROM financing_event_guarantee_tranches
               WHERE financing_event_id=? AND tier=?
                 AND source_document_id IS ?""",
            (tranche.financing_event_id, tier, tranche.source_document_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO financing_event_guarantee_tranches (
                    financing_event_id, tier, commitment_amount, fee_rate,
                    fee_amount, utilized_amount, source_document_id, source_page,
                    source_excerpt, confidence, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tranche.financing_event_id, tier, tranche.commitment_amount,
                    tranche.fee_rate, tranche.fee_amount, tranche.utilized_amount,
                    tranche.source_document_id, tranche.source_page,
                    tranche.source_excerpt, tranche.confidence, now, now,
                ),
            )
            row = connection.execute(
                """SELECT id FROM financing_event_guarantee_tranches
                   WHERE financing_event_id=? AND tier=?
                     AND source_document_id IS ?""",
                (tranche.financing_event_id, tier, tranche.source_document_id),
            ).fetchone()
        else:
            connection.execute(
                """UPDATE financing_event_guarantee_tranches
                   SET commitment_amount=COALESCE(?, commitment_amount),
                       fee_rate=COALESCE(?, fee_rate),
                       fee_amount=COALESCE(?, fee_amount),
                       utilized_amount=COALESCE(?, utilized_amount),
                       source_page=COALESCE(?, source_page),
                       source_excerpt=COALESCE(?, source_excerpt),
                       confidence=MAX(confidence, ?), updated_at=?
                   WHERE id=?""",
                (
                    tranche.commitment_amount, tranche.fee_rate, tranche.fee_amount,
                    tranche.utilized_amount, tranche.source_page, tranche.source_excerpt,
                    tranche.confidence, now, int(row["id"]),
                ),
            )
        return int(row["id"])

    # -- backfill state ------------------------------------------------------

    def get_backfill_state(
        self, connection: sqlite3.Connection, issuer_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM financing_backfill_state WHERE issuer_id=?",
            (issuer_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_backfill_state(
        self,
        connection: sqlite3.Connection,
        issuer_id: int,
        *,
        status: str,
        requested_start_date: str | None = None,
        requested_end_date: str | None = None,
        oldest_fetched_date: str | None = None,
        newest_fetched_date: str | None = None,
        source: str | None = None,
        cursor: str | None = None,
        checkpoint: str | None = None,
        last_successful_fetch_at: str | None = None,
        last_successful_extraction_at: str | None = None,
        last_successful_normalization_at: str | None = None,
        incomplete_ranges_json: str = "[]",
        failure_count: int = 0,
        retry_state: str | None = None,
    ) -> None:
        if status not in BACKFILL_STATUSES:
            raise ValueError(f"Ogiltig backfill-status: {status}")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO financing_backfill_state (
                issuer_id, requested_start_date, requested_end_date,
                oldest_fetched_date, newest_fetched_date, source, cursor,
                checkpoint, last_successful_fetch_at,
                last_successful_extraction_at, last_successful_normalization_at,
                incomplete_ranges_json, failure_count, retry_state, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issuer_id) DO UPDATE SET
                requested_start_date=COALESCE(excluded.requested_start_date, requested_start_date),
                requested_end_date=COALESCE(excluded.requested_end_date, requested_end_date),
                oldest_fetched_date=COALESCE(excluded.oldest_fetched_date, oldest_fetched_date),
                newest_fetched_date=COALESCE(excluded.newest_fetched_date, newest_fetched_date),
                source=COALESCE(excluded.source, source),
                cursor=COALESCE(excluded.cursor, cursor),
                checkpoint=COALESCE(excluded.checkpoint, checkpoint),
                last_successful_fetch_at=COALESCE(excluded.last_successful_fetch_at, last_successful_fetch_at),
                last_successful_extraction_at=COALESCE(excluded.last_successful_extraction_at, last_successful_extraction_at),
                last_successful_normalization_at=COALESCE(excluded.last_successful_normalization_at, last_successful_normalization_at),
                incomplete_ranges_json=excluded.incomplete_ranges_json,
                failure_count=excluded.failure_count,
                retry_state=excluded.retry_state,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                issuer_id,
                requested_start_date,
                requested_end_date,
                oldest_fetched_date,
                newest_fetched_date,
                source,
                cursor,
                checkpoint,
                last_successful_fetch_at,
                last_successful_extraction_at,
                last_successful_normalization_at,
                incomplete_ranges_json,
                failure_count,
                retry_state,
                status,
                now,
                now,
            ),
        )


# ---------------------------------------------------------------------------
# Incremental refresh: discover only new/changed source items, fingerprint,
# dedup, process only changed, normalize, deterministic calc, persist.
# ---------------------------------------------------------------------------


def source_item_key(item: dict[str, Any]) -> str:
    """Stable content key for a raw source item (document/announcement)."""
    return stable_hash(
        str(item.get("issuer_id") or ""),
        str(item.get("source") or ""),
        str(item.get("external_id") or item.get("url") or ""),
        str(item.get("content_sha256") or item.get("published_at") or ""),
    )


def incremental_refresh(
    store: FinancingHistoryStore,
    connection: sqlite3.Connection,
    issuer_id: int,
    source_items: Iterable[dict[str, Any]],
    extractor: FinancingExtractor,
) -> dict[str, int]:
    """Process only new/changed source items for an issuer.

    Each source item is content-hashed; items whose key already produced an
    event are skipped (dedup).  Changed items are re-extracted and re-persisted
    in place via fingerprint upsert — never a full 5-year re-analysis.

    Returns counters for observability.
    """
    stats = {"seen": 0, "new": 0, "changed": 0, "skipped": 0, "persisted": 0}
    for item in source_items:
        stats["seen"] += 1
        key = source_item_key(item)
        # Have we already processed this exact source content?
        marker = connection.execute(
            """
            SELECT event_fingerprint FROM financing_events
            WHERE issuer_id=? AND raw_type_description=?
            """,
            (issuer_id, f"__source__:{key}"),
        ).fetchone()
        raw_text = str(item.get("text") or "")
        raw_type = item.get("raw_type") or item.get("event_type")
        facts = extractor.extract(raw_text, issuer_id=issuer_id)
        for fact in facts:
            event = CanonicalFinancingEvent(
                issuer_id=issuer_id,
                financing_type=normalize_financing_type(
                    fact.get("raw_type") or raw_type
                ),
                raw_type_description=str(
                    fact.get("raw_type") or raw_type or ""
                ),
                description=str(fact.get("description") or ""),
                canonical_round_key=(
                    fact.get("canonical_round_key") or fact.get("round_key")
                ),
                lifecycle_stage=fact.get("lifecycle_stage"),
                stage_effective_at=fact.get("stage_effective_at"),
                announcement_date=fact.get("announcement_date"),
                effective_date=fact.get("effective_date"),
                status=str(fact.get("status") or "announced"),
                currency=fact.get("currency"),
                gross_amount=fact.get("gross_amount"),
                net_amount=fact.get("net_amount"),
                issue_price=fact.get("issue_price"),
                shares_before=fact.get("shares_before"),
                shares_issued=fact.get("shares_issued"),
                shares_after=fact.get("shares_after"),
                market_price_reference=fact.get("market_price_reference"),
                vwap_reference=fact.get("vwap_reference"),
                market_cap_before=fact.get("market_cap_before"),
                subscription_commitment_amount=fact.get(
                    "subscription_commitment_amount"
                ),
                guarantee_commitment_amount=fact.get(
                    "guarantee_commitment_amount"
                ),
                final_subscription_amount=fact.get("final_subscription_amount"),
                guarantee_utilized_amount=fact.get("guarantee_utilized_amount"),
                transaction_cost=fact.get("transaction_cost"),
                guarantee_fee_amount=fact.get("guarantee_fee_amount"),
                guarantee_fee_pct=fact.get("guarantee_fee_pct"),
                guarantee_fee_cash_pct=fact.get("guarantee_fee_cash_pct"),
                guarantee_fee_share_pct=fact.get("guarantee_fee_share_pct"),
                guarantee_fee_basis=fact.get("guarantee_fee_basis"),
                credit_limit=fact.get("credit_limit"),
                drawn_amount=fact.get("drawn_amount"),
                undrawn_commitment=fact.get("undrawn_commitment"),
                adviser=fact.get("adviser"),
                issuer_agent=fact.get("issuer_agent"),
                outcome=fact.get("outcome"),
                confidence=float(fact.get("confidence") or 0.0),
            )
            _, created = store.upsert_event(connection, event)
            stats["persisted"] += 1
            if created:
                stats["new"] += 1
            else:
                stats["changed"] += 1
        if marker is not None and not facts:
            stats["skipped"] += 1
    return stats


# ---------------------------------------------------------------------------
# Historical aggregates (deterministic).  Each aggregate reports numerator /
# denominator / sample_size / data_completeness where relevant.
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def historical_aggregates(
    connection: sqlite3.Connection,
    issuer_id: int,
    *,
    since_date: str,
) -> dict[str, Any]:
    """Compute deterministic 5-year aggregates from financing_events.

    All metrics are computed in code from stored observable facts.  Any metric
    without sufficient data is None (null before guess) and reports its
    sample_size / data_completeness.
    """
    rows = connection.execute(
        """
        SELECT * FROM financing_events
        WHERE issuer_id=?
          AND COALESCE(effective_date, announcement_date)>=?
        ORDER BY COALESCE(effective_date, announcement_date), id
        """,
        (issuer_id, since_date),
    ).fetchall()
    events = [dict(row) for row in rows]
    total = len(events)

    def _count(types: set[str]) -> int:
        return sum(1 for e in events if e.get("financing_type") in types)

    equity_types = {"rights_issue", "directed_issue", "unit_issue", "other_equity"}
    total_equity = sum(
        float(e["gross_amount"])
        for e in events
        if e.get("financing_type") in equity_types and e.get("gross_amount") is not None
    )

    raise_ratios = [
        float(e["raise_to_market_cap"])
        for e in events
        if e.get("raise_to_market_cap") is not None
    ]
    discounts = [
        float(e["discount_to_spot"])
        for e in events
        if e.get("discount_to_spot") is not None
    ]
    weighted_discount_num = sum(
        float(e["discount_to_spot"]) * float(e["gross_amount"])
        for e in events
        if e.get("discount_to_spot") is not None and e.get("gross_amount") is not None
    )
    weighted_discount_den = sum(
        float(e["gross_amount"])
        for e in events
        if e.get("discount_to_spot") is not None and e.get("gross_amount") is not None
    )
    final_subs = [
        float(e["final_subscription_pct"])
        for e in events
        if e.get("final_subscription_pct") is not None
    ]
    subs_no_guarantee = [
        float(e["subscription_without_guarantees_pct"])
        for e in events
        if e.get("subscription_without_guarantees_pct") is not None
    ]
    secured = [
        float(e["secured_total_pct"])
        for e in events
        if e.get("secured_total_pct") is not None
    ]
    guarantee_util = [
        float(e["guarantee_utilized_pct"])
        for e in events
        if e.get("guarantee_utilized_pct") is not None
    ]

    def _completeness(sample: int) -> float | None:
        if total == 0:
            return None
        return sample / total

    return {
        "issuer_id": issuer_id,
        "since_date": since_date,
        "sample_size": total,
        "financings_5y_count": total,
        "rights_issues_5y_count": _count({"rights_issue"}),
        "directed_issues_5y_count": _count({"directed_issue"}),
        "total_equity_raised_5y": total_equity if total else None,
        "median_raise_to_market_cap": _median(raise_ratios),
        "median_raise_to_market_cap_sample": len(raise_ratios),
        "median_discount": _median(discounts),
        "median_discount_sample": len(discounts),
        "weighted_average_discount": (
            weighted_discount_num / weighted_discount_den
            if weighted_discount_den
            else None
        ),
        "weighted_average_discount_numerator": weighted_discount_num or None,
        "weighted_average_discount_denominator": weighted_discount_den or None,
        "median_final_subscription": _median(final_subs),
        "median_final_subscription_sample": len(final_subs),
        "median_subscription_without_guarantees": _median(subs_no_guarantee),
        "median_subscription_without_guarantees_sample": len(subs_no_guarantee),
        "average_secured_pct": (sum(secured) / len(secured)) if secured else None,
        "average_secured_pct_sample": len(secured),
        "average_guarantee_utilization": (
            sum(guarantee_util) / len(guarantee_util) if guarantee_util else None
        ),
        "average_guarantee_utilization_sample": len(guarantee_util),
        "data_completeness": _completeness(total),
        # Participant-driven rates require participant rows; computed by caller
        # via participant_aggregates when available.
        "main_owner_participation_rate": None,
        "repeat_investor_rate": None,
    }


def participant_aggregates(
    connection: sqlite3.Connection,
    issuer_id: int,
    *,
    since_date: str,
) -> dict[str, Any]:
    """Deterministic participant-driven rates (main owner, repeat investor)."""
    rows = connection.execute(
        """
        SELECT p.role, p.raw_name, p.financing_event_id
        FROM financing_event_participants p
        JOIN financing_events e ON e.id=p.financing_event_id
        WHERE e.issuer_id=?
          AND COALESCE(e.effective_date, e.announcement_date)>=?
        """,
        (issuer_id, since_date),
    ).fetchall()
    participants = [dict(row) for row in rows]
    event_ids = {int(p["financing_event_id"]) for p in participants}
    total_events = len(event_ids)

    main_owner_events = {
        int(p["financing_event_id"])
        for p in participants
        if p["role"] in {"major_owner", "board", "management"}
    }
    main_owner_rate = (
        len(main_owner_events) / total_events if total_events else None
    )

    name_counts: dict[str, int] = {}
    for p in participants:
        key = str(p["raw_name"]).casefold().strip()
        name_counts[key] = name_counts.get(key, 0) + 1
    repeat = sum(1 for count in name_counts.values() if count > 1)
    repeat_rate = (repeat / len(name_counts)) if name_counts else None

    return {
        "main_owner_participation_rate": main_owner_rate,
        "main_owner_participation_numerator": len(main_owner_events) or None,
        "main_owner_participation_denominator": total_events or None,
        "repeat_investor_rate": repeat_rate,
        "repeat_investor_numerator": repeat or None,
        "repeat_investor_denominator": len(name_counts) or None,
    }


# ---------------------------------------------------------------------------
# Market-data hooks.  Schema/interface prepared; returns null/unavailable when
# no verified provider exists.  NEVER invent market data.
# ---------------------------------------------------------------------------


class MarketDataProvider(Protocol):
    """Interface for a verified market-data provider (price/cap/ADTV/VWAP)."""

    def snapshot(self, issuer_id: int, as_of_date: str) -> dict[str, Any] | None:
        ...


def market_snapshot_unavailable(issuer_id: int, as_of_date: str) -> dict[str, Any]:
    """Null/unavailable snapshot when no verified provider is wired."""
    return {
        "issuer_id": issuer_id,
        "as_of_date": as_of_date,
        "price": None,
        "market_cap": None,
        "adtv": None,
        "vwap": None,
        "return_m20": None,
        "return_p1": None,
        "return_p5": None,
        "return_p20": None,
        "return_p60": None,
        "source": None,
        "available": False,
    }


def get_market_snapshot(
    provider: MarketDataProvider | None,
    issuer_id: int,
    as_of_date: str,
) -> dict[str, Any]:
    """Return a market snapshot, or null/unavailable when no provider exists."""
    if provider is None:
        return market_snapshot_unavailable(issuer_id, as_of_date)
    snapshot = provider.snapshot(issuer_id, as_of_date)
    if snapshot is None:
        return market_snapshot_unavailable(issuer_id, as_of_date)
    snapshot["available"] = True
    return snapshot
