from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.db import Database
from capital_radar.analysis_store import AnalysisStore
from capital_radar.extraction_v2 import _persist_financing_events
from capital_radar.financing_history import (
    FINANCING_EVENTS_ENSURE_COLUMNS,
    FINANCING_HISTORY_MIGRATION_SQL,
    FINANCING_HISTORY_NEW_TABLES_SQL,
    CanonicalFinancingEvent,
    GuaranteeTranche,
    FinancingHistoryStore,
    FinancingParticipant,
    compute_derived_metrics,
    get_market_snapshot,
    historical_aggregates,
    incremental_refresh,
    normalize_canonical_round_key,
    normalize_lifecycle_stage,
    normalize_financing_type,
    participant_aggregates,
)
from capital_radar.normalization import utc_now


def _make_db(directory: str) -> Database:
    db = Database(Path(directory) / "test.db")
    db.initialize()
    return db


def _create_issuer(db: Database, name: str = "Exempel AB") -> int:
    now = utc_now()
    with db.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO issuers (
                canonical_name, normalized_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (name, name.casefold(), now, now),
        )
        return int(cursor.lastrowid)


class _FakeExtractor:
    """Fake AI extractor returning raw observable facts only (no math)."""

    def __init__(self, facts: list[dict]) -> None:
        self._facts = facts
        self.calls = 0

    def extract(self, document_text: str, *, issuer_id: int) -> list[dict]:
        self.calls += 1
        return list(self._facts)


class NormalizationTests(unittest.TestCase):
    def test_raw_type_maps_to_canonical_and_raw_kept(self) -> None:
        self.assertEqual(normalize_financing_type("riktad emission"), "directed_issue")
        self.assertEqual(
            normalize_financing_type("Företrädesemission"), "rights_issue"
        )
        self.assertEqual(normalize_financing_type("konvertibelt lån"), "convertible")
        self.assertEqual(normalize_financing_type("något helt okänt"), "unknown")
        self.assertEqual(normalize_financing_type(None), "unknown")

    def test_substring_match_for_longer_description(self) -> None:
        self.assertEqual(
            normalize_financing_type("Bolaget genomför en riktad emission om 10 MSEK"),
            "directed_issue",
        )

    def test_lifecycle_stage_requires_an_explicit_known_value(self) -> None:
        self.assertEqual(
            normalize_lifecycle_stage("outcome published"),
            "outcome_published",
        )
        self.assertIsNone(normalize_lifecycle_stage(None))
        with self.assertRaises(ValueError):
            normalize_lifecycle_stage("probably settled")

    def test_round_key_is_normalized_but_not_inferred(self) -> None:
        self.assertEqual(
            normalize_canonical_round_key(" Issuer-1  Rights-2026 "),
            "issuer-1-rights-2026",
        )
        self.assertIsNone(normalize_canonical_round_key(None))


class DerivedMetricsTests(unittest.TestCase):
    def test_dilution_from_shares_after(self) -> None:
        event = CanonicalFinancingEvent(
            issuer_id=1,
            financing_type="directed_issue",
            raw_type_description="riktad emission",
            description="test",
            shares_issued=1_000_000,
            shares_after=11_000_000,
        )
        compute_derived_metrics(event)
        self.assertAlmostEqual(event.dilution, 1_000_000 / 11_000_000)

    def test_dilution_fallback_before_plus_issued(self) -> None:
        event = CanonicalFinancingEvent(
            issuer_id=1,
            financing_type="rights_issue",
            raw_type_description="företrädesemission",
            description="test",
            shares_before=10_000_000,
            shares_issued=2_000_000,
        )
        compute_derived_metrics(event)
        self.assertAlmostEqual(event.dilution, 2_000_000 / 12_000_000)

    def test_raise_to_market_cap_and_discounts(self) -> None:
        event = CanonicalFinancingEvent(
            issuer_id=1,
            financing_type="directed_issue",
            raw_type_description="riktad emission",
            description="test",
            gross_amount=50_000_000,
            market_cap_before=500_000_000,
            issue_price=9.0,
            market_price_reference=10.0,
            vwap_reference=9.5,
        )
        compute_derived_metrics(event)
        self.assertAlmostEqual(event.raise_to_market_cap, 0.10)
        self.assertAlmostEqual(event.discount_to_spot, 0.10)
        self.assertAlmostEqual(event.discount_to_vwap, (9.5 - 9.0) / 9.5)

    def test_secured_pct_and_guarantee_utilization(self) -> None:
        event = CanonicalFinancingEvent(
            issuer_id=1,
            financing_type="rights_issue",
            raw_type_description="företrädesemission",
            description="test",
            gross_amount=100_000_000,
            subscription_commitment_amount=60_000_000,
            guarantee_commitment_amount=20_000_000,
            guarantee_utilized_amount=10_000_000,
            final_subscription_amount=85_000_000,
            transaction_cost=5_000_000,
        )
        compute_derived_metrics(event)
        self.assertAlmostEqual(event.subscription_commitment_pct, 0.60)
        self.assertAlmostEqual(event.guarantee_commitment_pct, 0.20)
        self.assertAlmostEqual(event.secured_total_amount, 80_000_000)
        self.assertAlmostEqual(event.secured_total_pct, 0.80)
        self.assertAlmostEqual(event.final_subscription_pct, 0.85)
        self.assertAlmostEqual(event.guarantee_utilized_pct, 0.50)
        self.assertAlmostEqual(event.transaction_cost_pct, 0.05)
        # subscription without guarantees = (85M - 10M) / 100M
        self.assertAlmostEqual(event.subscription_without_guarantees_pct, 0.75)

    def test_null_before_guess_when_inputs_missing(self) -> None:
        event = CanonicalFinancingEvent(
            issuer_id=1,
            financing_type="unknown",
            raw_type_description="",
            description="test",
        )
        compute_derived_metrics(event)
        self.assertIsNone(event.dilution)
        self.assertIsNone(event.raise_to_market_cap)
        self.assertIsNone(event.discount_to_spot)
        self.assertIsNone(event.secured_total_pct)
        self.assertIsNone(event.guarantee_utilized_pct)


class StoreSchemaTests(unittest.TestCase):
    def test_ensure_schema_is_additive_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                # Second run must be a no-op (idempotent).
                store.ensure_schema(connection)
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(financing_events)"
                    )
                }
                for expected in FINANCING_EVENTS_ENSURE_COLUMNS:
                    self.assertIn(expected, columns)
                for table in (
                    "financing_event_relationships",
                    "financing_event_participants",
                    "financing_backfill_state",
                    "financing_market_snapshots",
                    "financing_event_guarantee_tranches",
                    "financing_event_enrichment_facts",
                ):
                    row = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    self.assertIsNotNone(row, table)

    def test_migration_sql_constant_contains_columns_and_tables(self) -> None:
        self.assertIn("financing_type", FINANCING_HISTORY_MIGRATION_SQL)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS financing_event_participants",
            FINANCING_HISTORY_NEW_TABLES_SQL,
        )
        self.assertIn(
            "financing_backfill_state", FINANCING_HISTORY_MIGRATION_SQL
        )


class DedupTests(unittest.TestCase):
    def test_upsert_dedups_via_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                event = CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="directed_issue",
                    raw_type_description="riktad emission",
                    description="Riktad emission om 50 MSEK",
                    announcement_date="2024-01-15",
                    gross_amount=50_000_000,
                    currency="SEK",
                )
                first_id, created_first = store.upsert_event(connection, event)
                self.assertTrue(created_first)
                # Same event again -> same fingerprint, update in place.
                second_id, created_second = store.upsert_event(connection, event)
                self.assertFalse(created_second)
                self.assertEqual(first_id, second_id)
                count = connection.execute(
                    "SELECT COUNT(*) AS c FROM financing_events WHERE issuer_id=?",
                    (issuer_id,),
                ).fetchone()["c"]
                self.assertEqual(count, 1)

    def test_round_and_lifecycle_fields_are_persisted_without_affecting_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                event = CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="rights_issue",
                    raw_type_description="företrädesemission",
                    description="Villkor för företrädesemission",
                    announcement_date="2026-09-10",
                    gross_amount=10_000_000,
                    currency="SEK",
                    canonical_round_key="issuer-1-rights-2026-09",
                    lifecycle_stage="announced",
                    stage_effective_at="2026-09-10T08:00:00Z",
                    guarantee_commitment_amount=5_000_000,
                    guarantee_fee_amount=600_000,
                )
                compute_derived_metrics(event)
                event_id, _ = store.upsert_event(connection, event)
                row = connection.execute(
                    """SELECT canonical_round_key, lifecycle_stage, stage_effective_at,
                              guarantee_fee_amount, guarantee_fee_pct
                       FROM financing_events WHERE id=?""",
                    (event_id,),
                ).fetchone()
                self.assertEqual(row["canonical_round_key"], "issuer-1-rights-2026-09")
                self.assertEqual(row["lifecycle_stage"], "announced")
                self.assertEqual(row["stage_effective_at"], "2026-09-10T08:00:00Z")
                self.assertEqual(row["guarantee_fee_amount"], 600_000)
                self.assertAlmostEqual(row["guarantee_fee_pct"], 0.12)

    def test_guarantee_tranche_is_idempotent_and_source_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                event_id, _ = store.upsert_event(
                    connection,
                    CanonicalFinancingEvent(
                        issuer_id=issuer_id,
                        financing_type="rights_issue",
                        raw_type_description="företrädesemission",
                        description="Villkor för emission",
                        announcement_date="2026-09-10",
                        gross_amount=10_000_000,
                        currency="SEK",
                    ),
                )
                first_id = store.record_guarantee_tranche(
                    connection,
                    GuaranteeTranche(
                        financing_event_id=event_id,
                        tier="top",
                        commitment_amount=4_000_000,
                        fee_rate=0.14,
                        source_excerpt="Top guarantee receives 14 percent.",
                        confidence=0.9,
                    ),
                )
                second_id = store.record_guarantee_tranche(
                    connection,
                    GuaranteeTranche(
                        financing_event_id=event_id,
                        tier="top",
                        fee_amount=560_000,
                        source_excerpt="Fee is SEK 560,000.",
                        confidence=0.95,
                    ),
                )
                self.assertEqual(first_id, second_id)
                row = connection.execute(
                    """SELECT tier, commitment_amount, fee_rate, fee_amount,
                              source_excerpt, confidence
                       FROM financing_event_guarantee_tranches WHERE id=?""",
                    (first_id,),
                ).fetchone()
                self.assertEqual(row["tier"], "top")
                self.assertEqual(row["commitment_amount"], 4_000_000)
                self.assertEqual(row["fee_rate"], 0.14)
                self.assertEqual(row["fee_amount"], 560_000)
                self.assertEqual(row["source_excerpt"], "Fee is SEK 560,000.")
                self.assertEqual(row["confidence"], 0.95)

    def test_v2_extraction_persists_structured_guarantee_tranche(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            written = _persist_financing_events(
                AnalysisStore(db),
                issuer_id=issuer_id,
                document_id=None,
                job_id=0,
                parsed={
                    "financing_events": [{
                        "event_type": "rights_issue",
                        "description": "Företrädesemission",
                        "status": "announced",
                        "cash_direction": "inflow",
                        "gross_amount": 10_000_000,
                        "currency": "SEK",
                        "liquidity_effect_kind": "conditional_liquidity",
                        "reflected_in_report": "no",
                        "confidence": 0.9,
                        "guarantee_tranches": [{
                            "tier": "middle",
                            "commitment_amount": 4_000_000,
                            "fee_rate": 0.12,
                            "utilized_amount": 500_000,
                            "source_excerpt": "Middle guarantee: 12 percent.",
                            "confidence": 0.95,
                        }],
                    }],
                },
            )
            self.assertEqual(written, 1)
            with db.connect() as connection:
                row = connection.execute(
                    """SELECT tier, commitment_amount, fee_rate, utilized_amount,
                              source_excerpt
                       FROM financing_event_guarantee_tranches"""
                ).fetchone()
            self.assertEqual(row["tier"], "middle")
            self.assertEqual(row["commitment_amount"], 4_000_000)
            self.assertEqual(row["fee_rate"], 0.12)
            self.assertEqual(row["utilized_amount"], 500_000)
            self.assertEqual(row["source_excerpt"], "Middle guarantee: 12 percent.")

    def test_round_stages_and_bridge_relationship_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                common = dict(
                    issuer_id=issuer_id,
                    financing_type="rights_issue",
                    raw_type_description="företrädesemission",
                    description="Företrädesemission om 10 MSEK",
                    announcement_date="2026-09-10",
                    gross_amount=10_000_000,
                    currency="SEK",
                    canonical_round_key="issuer-1-rights-2026-09",
                )
                terms_id, _ = store.upsert_event(
                    connection,
                    CanonicalFinancingEvent(**common, lifecycle_stage="terms_published"),
                )
                outcome_id, _ = store.upsert_event(
                    connection,
                    CanonicalFinancingEvent(**common, lifecycle_stage="outcome_published"),
                )
                self.assertNotEqual(terms_id, outcome_id)
                relationship_id = store.record_relationship(
                    connection,
                    financing_event_id=terms_id,
                    related_financing_event_id=outcome_id,
                    relationship_type="repaid_by",
                    source="issuer release",
                    confidence=0.9,
                )
                self.assertGreater(relationship_id, 0)


class BackfillStateTests(unittest.TestCase):
    def test_backfill_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                self.assertIsNone(
                    store.get_backfill_state(connection, issuer_id)
                )
                store.upsert_backfill_state(
                    connection, issuer_id, status="not_started"
                )
                state = store.get_backfill_state(connection, issuer_id)
                self.assertEqual(state["status"], "not_started")

                store.upsert_backfill_state(
                    connection,
                    issuer_id,
                    status="in_progress",
                    requested_start_date="2021-01-01",
                    requested_end_date="2026-01-01",
                    oldest_fetched_date="2021-03-01",
                )
                state = store.get_backfill_state(connection, issuer_id)
                self.assertEqual(state["status"], "in_progress")
                self.assertEqual(state["oldest_fetched_date"], "2021-03-01")

                store.upsert_backfill_state(
                    connection,
                    issuer_id,
                    status="completed",
                    newest_fetched_date="2026-01-01",
                )
                state = store.get_backfill_state(connection, issuer_id)
                self.assertEqual(state["status"], "completed")
                # Earlier fields preserved via COALESCE.
                self.assertEqual(state["requested_start_date"], "2021-01-01")

    def test_invalid_backfill_status_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                with self.assertRaises(ValueError):
                    store.upsert_backfill_state(
                        connection, issuer_id, status="bogus"
                    )


class IncrementalRefreshTests(unittest.TestCase):
    def test_only_changed_items_are_processed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            facts = [
                {
                    "raw_type": "riktad emission",
                    "description": "Riktad emission",
                    "announcement_date": "2024-02-01",
                    "canonical_round_key": "issuer-1-directed-2024-02",
                    "lifecycle_stage": "announced",
                    "stage_effective_at": "2024-02-01T07:00:00Z",
                    "gross_amount": 30_000_000,
                    "currency": "SEK",
                    "confidence": 0.9,
                }
            ]
            extractor = _FakeExtractor(facts)
            items = [
                {
                    "issuer_id": issuer_id,
                    "source": "mfn",
                    "external_id": "mfn-1",
                    "content_sha256": "abc",
                    "text": "Riktad emission om 30 MSEK",
                    "raw_type": "riktad emission",
                }
            ]
            with db.connect() as connection:
                store.ensure_schema(connection)
                first = incremental_refresh(
                    store, connection, issuer_id, items, extractor
                )
                self.assertEqual(first["seen"], 1)
                self.assertEqual(first["new"], 1)
                self.assertEqual(extractor.calls, 1)

                # Re-running with the SAME content must not create duplicates.
                second = incremental_refresh(
                    store, connection, issuer_id, items, extractor
                )
                self.assertEqual(second["new"], 0)
                row = connection.execute(
                    """SELECT COUNT(*) AS c, canonical_round_key, lifecycle_stage
                       FROM financing_events WHERE issuer_id=?""",
                    (issuer_id,),
                ).fetchone()
                self.assertEqual(row["c"], 1)
                self.assertEqual(row["canonical_round_key"], "issuer-1-directed-2024-02")
                self.assertEqual(row["lifecycle_stage"], "announced")


class ParticipantTests(unittest.TestCase):
    def test_participants_recorded_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                event = CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="rights_issue",
                    raw_type_description="företrädesemission",
                    description="Företrädesemission",
                    announcement_date="2024-03-01",
                    gross_amount=100_000_000,
                    currency="SEK",
                )
                event_id, _ = store.upsert_event(connection, event)
                participant = FinancingParticipant(
                    financing_event_id=event_id,
                    raw_name="Storägare X",
                    role="major_owner",
                    amount=40_000_000,
                    confidence=0.8,
                )
                pid = store.record_participant(connection, participant)
                self.assertGreater(pid, 0)
                row = connection.execute(
                    "SELECT * FROM financing_event_participants WHERE id=?",
                    (pid,),
                ).fetchone()
                # Conservative: unresolved unless a confident candidate given.
                self.assertEqual(row["resolution_status"], "unresolved")
                self.assertEqual(row["raw_name"], "Storägare X")


class AggregateTests(unittest.TestCase):
    def _seed_events(self, db: Database, issuer_id: int) -> None:
        store = FinancingHistoryStore(db)
        with db.connect() as connection:
            store.ensure_schema(connection)
            events = [
                CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="rights_issue",
                    raw_type_description="företrädesemission",
                    description="FE 2022",
                    announcement_date="2022-01-01",
                    gross_amount=100_000_000,
                    market_cap_before=1_000_000_000,
                    currency="SEK",
                ),
                CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="directed_issue",
                    raw_type_description="riktad emission",
                    description="RE 2023",
                    announcement_date="2023-01-01",
                    gross_amount=50_000_000,
                    market_cap_before=500_000_000,
                    currency="SEK",
                ),
                CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="directed_issue",
                    raw_type_description="riktad emission",
                    description="RE 2024",
                    announcement_date="2024-01-01",
                    gross_amount=80_000_000,
                    market_cap_before=400_000_000,
                    currency="SEK",
                ),
            ]
            for event in events:
                store.upsert_event(connection, event)

    def test_historical_aggregates_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            self._seed_events(db, issuer_id)
            with db.connect() as connection:
                agg = historical_aggregates(
                    connection, issuer_id, since_date="2021-01-01"
                )
            self.assertEqual(agg["financings_5y_count"], 3)
            self.assertEqual(agg["rights_issues_5y_count"], 1)
            self.assertEqual(agg["directed_issues_5y_count"], 2)
            self.assertEqual(agg["total_equity_raised_5y"], 230_000_000)
            # raise/market_cap ratios: 0.10, 0.10, 0.20 -> median 0.10
            self.assertAlmostEqual(agg["median_raise_to_market_cap"], 0.10)
            self.assertEqual(agg["median_raise_to_market_cap_sample"], 3)
            self.assertEqual(agg["sample_size"], 3)

    def test_aggregates_null_when_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                agg = historical_aggregates(
                    connection, issuer_id, since_date="2021-01-01"
                )
            self.assertEqual(agg["financings_5y_count"], 0)
            self.assertIsNone(agg["median_raise_to_market_cap"])
            self.assertIsNone(agg["total_equity_raised_5y"])

    def test_participant_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _make_db(directory)
            issuer_id = _create_issuer(db)
            store = FinancingHistoryStore(db)
            with db.connect() as connection:
                store.ensure_schema(connection)
                event = CanonicalFinancingEvent(
                    issuer_id=issuer_id,
                    financing_type="rights_issue",
                    raw_type_description="företrädesemission",
                    description="FE",
                    announcement_date="2023-06-01",
                    gross_amount=100_000_000,
                    currency="SEK",
                )
                event_id, _ = store.upsert_event(connection, event)
                store.record_participant(
                    connection,
                    FinancingParticipant(
                        financing_event_id=event_id,
                        raw_name="Ägare A",
                        role="major_owner",
                    ),
                )
                store.record_participant(
                    connection,
                    FinancingParticipant(
                        financing_event_id=event_id,
                        raw_name="Ägare A",
                        role="subscription_commitment",
                    ),
                )
                agg = participant_aggregates(
                    connection, issuer_id, since_date="2021-01-01"
                )
            self.assertEqual(agg["main_owner_participation_rate"], 1.0)
            # "Ägare A" appears twice -> repeat investor.
            self.assertEqual(agg["repeat_investor_rate"], 1.0)


class MarketDataTests(unittest.TestCase):
    def test_no_provider_returns_unavailable_null_snapshot(self) -> None:
        snapshot = get_market_snapshot(None, issuer_id=1, as_of_date="2024-01-01")
        self.assertFalse(snapshot["available"])
        self.assertIsNone(snapshot["price"])
        self.assertIsNone(snapshot["market_cap"])
        self.assertIsNone(snapshot["vwap"])
        self.assertIsNone(snapshot["return_p20"])


if __name__ == "__main__":
    unittest.main()
