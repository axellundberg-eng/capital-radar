"""End-to-end, source-backed transaction value correction on real SQLite."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.models import DocumentCandidate
from capital_radar.radar_analysis import current_analysis
from capital_radar.transaction_feed import get_transaction_detail
from capital_radar.transaction_value_corrections import (
    TransactionValueCorrectionError,
    apply_transaction_value_correction,
    approve_transaction_value_correction,
    create_transaction_value_correction,
    revert_transaction_value_correction,
    transaction_value_correction_detail,
)


class TransactionValueCorrectionTests(unittest.TestCase):
    def test_audited_announced_gross_updates_display_ratio_and_reverts_with_originals(self):
        from capital_radar.transaction_feed import _build_transaction_items
        body = 'Bolaget tillförs 20 MSEK före emissionskostnader i företrädesemissionen.'
        self.db.upsert_document(DocumentCandidate(
            source_id='mfn', external_id='realfiction-outcome', issuer_name='Realfiction AB',
            title='Realfiction offentliggör utfall i företrädesemission',
            canonical_url='https://mfn.se/a/realfiction/outcome',
            published_at='2026-09-25T08:00:00Z', language='sv'), universe_only=False)
        with self.db.connect() as connection:
            doc = connection.execute('SELECT id FROM documents ORDER BY id DESC LIMIT 1').fetchone()[0]
            connection.execute("""INSERT INTO document_contents
                (document_id,source_url,extracted_text,text_sha256,char_count,status,created_at,updated_at)
                VALUES (?,?,?,?,?,'extracted','2026-09-25','2026-09-25')""",
                (doc, 'https://mfn.se/a/realfiction/outcome', body,
                 hashlib.sha256(body.encode()).hexdigest(), len(body)))
            items = _build_transaction_items(connection, issuer_ids=[self.issuer_id])
            self.assertEqual(len(items), 1)
            initial = items[0]
            self.assertEqual(initial['amount'], 20_000_000)
            self.assertEqual(initial['details']['announced_amount'], 30_000_000)
            self.assertEqual(initial['details']['completion_ratio_pct'], 66.7)
            correction = create_transaction_value_correction(connection, {
                'transaction_id': initial['id'], 'issuer_id': self.issuer_id,
                'field_name': 'announced_gross_amount',
                'expected_previous_value': initial['effective_transaction_values']['announced_gross_amount'],
                'proposed_value': 36_000_000, 'source_document_id': self.document_id,
                'source_excerpt': self.source, 'confidence': .99,
                'rationale': 'Källan anger annonserat brutto36M, separat från faktiskt utfall20M.'},
                actor='review-agent')
            approve_transaction_value_correction(connection, correction['id'], actor='review-agent')
            apply_transaction_value_correction(connection, correction['id'], actor='review-agent')
            card = get_transaction_detail(connection, initial['id'])
            self.assertEqual(card['details']['announced_amount'], 36_000_000)
            self.assertEqual(card['details']['original_announced_amount'], 30_000_000)
            self.assertEqual(card['details']['completion_ratio_pct'], 55.6)
            self.assertEqual(card['details']['original_completion_ratio_pct'], 66.7)
            self.assertEqual(card['amount'], 20_000_000)
            self.assertEqual(card['source_document_ids'], initial['source_document_ids'])
            self.assertEqual(card['original_transaction_values'], initial['original_transaction_values'])
            self.assertEqual(card['details']['confirmed_cash_effect_amount'],
                             initial['details']['confirmed_cash_effect_amount'])
            self.assertEqual(current_analysis(connection, [self.issuer_id])[0]['reported_runway_months'], 10)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_events').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT extracted_text FROM document_contents WHERE document_id=?',
                                               (self.document_id,)).fetchone()[0], self.source)
            revert_transaction_value_correction(connection, correction['id'], actor='review-agent')
            reverted = get_transaction_detail(connection, initial['id'])
            self.assertEqual(reverted['details']['announced_amount'], 30_000_000)
            self.assertEqual(reverted['details']['completion_ratio_pct'], 66.7)

    def test_gross_display_projection_preserves_net_limits_commitments_and_unknown_ratios(self):
        from copy import deepcopy
        from capital_radar.transaction_feed import _build_transaction_items, _project_transaction_values
        with self.db.connect() as connection:
            base = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
        for basis in ('announced_net', 'credit_limit', 'guarantee_commitment'):
            with self.subTest(basis=basis):
                card = deepcopy(base)
                card['details'].update(announced_amount=12, announced_amount_basis=basis,
                    outcome_amount=6, outcome_amount_basis='outcome_gross', completion_ratio_pct=50)
                _project_transaction_values(card, {'values': {'announced_gross_amount': 36}})
                self.assertEqual(card['details']['announced_amount'], 12)
                self.assertEqual(card['details']['completion_ratio_pct'], 50)
                self.assertEqual(card['details']['confirmed_cash_effect_amount'],
                                 base['details']['confirmed_cash_effect_amount'])
        card = deepcopy(base)
        card['details'].update(announced_amount=12, announced_amount_basis='announced_gross',
            outcome_amount=6, outcome_amount_basis='outcome_gross', completion_ratio_pct=None)
        _project_transaction_values(card, {'values': {'announced_gross_amount': 36}})
        self.assertEqual(card['details']['announced_amount'], 36)
        self.assertIsNone(card['details']['completion_ratio_pct'])

    def test_unknown_bases_do_not_allow_clearing_identity_or_structured_fields(self):
        from capital_radar.transaction_value_corrections import _validate_proposed
        for field in ('guarantee_fee_basis', 'transaction_cost_basis', 'net_amount_basis'):
            self.assertIsNone(_validate_proposed(field, None))
            with self.subTest(structured_basis=field), self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed(field, None, allow_unknown=False)
        for field in ('currency', 'category', 'status', 'lifecycle_stage',
                      'canonical_round_key', 'amount_basis',
                      'additional_instruments', 'effective_stages'):
            with self.subTest(protected_field=field), self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed(field, None)

    def test_unknown_basis_overlays_apply_and_restore_without_changing_source_or_runway(self):
        from capital_radar.transaction_feed import _build_transaction_items
        with self.db.connect() as connection:
            initial = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            def propose(field, value, previous):
                created = create_transaction_value_correction(connection, {
                    'transaction_id': initial['id'], 'issuer_id': self.issuer_id,
                    'field_name': field, 'expected_previous_value': previous,
                    'proposed_value': value, 'source_document_id': self.document_id,
                    'source_excerpt': self.source, 'confidence': .99,
                    'rationale': 'Den gamla basen saknar källstöd för just detta instrument.'},
                    actor='review-agent')
                approve_transaction_value_correction(connection, created['id'], actor='review-agent')
                result = apply_transaction_value_correction(connection, created['id'], actor='review-agent')
                self.assertEqual(result['status'], 'applied')
                return created['id']
            for field, old_basis in (('guarantee_fee_basis', 'guaranteed_amount'),
                                     ('transaction_cost_basis', 'source_reported'),
                                     ('net_amount_basis', 'derived_gross_less_source_costs')):
                previous = get_transaction_detail(connection, initial['id'])['effective_transaction_values'][field]
                old = propose(field, old_basis, previous)
                cleared = propose(field, None, old_basis)
                card = get_transaction_detail(connection, initial['id'])
                self.assertIsNone(card['effective_transaction_values'][field])
                dossier = card['details']['financing_dossier']
                if field == 'guarantee_fee_basis':
                    self.assertIsNone(dossier['guarantee_fee_options']['fee_basis'])
                else:
                    self.assertIsNone(dossier[field])
                self.assertEqual(card['original_transaction_values'], initial['original_transaction_values'])
                self.assertEqual(card['source_document_ids'], initial['source_document_ids'])
                self.assertEqual(card['amount'], initial['amount'])
                self.assertEqual(card['details']['confirmed_cash_effect_amount'],
                                 initial['details']['confirmed_cash_effect_amount'])
                self.assertEqual(current_analysis(connection, [self.issuer_id])[0]['reported_runway_months'], 10)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_events').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT extracted_text FROM document_contents WHERE document_id=?',
                                                   (self.document_id,)).fetchone()[0], self.source)
                revert_transaction_value_correction(connection, cleared, actor='review-agent')
                self.assertEqual(get_transaction_detail(connection, initial['id'])['effective_transaction_values'][field],
                                 old_basis)
                revert_transaction_value_correction(connection, old, actor='review-agent')

    def test_scalar_unknown_is_distinct_from_zero_and_structured_amounts_stay_required(self):
        from capital_radar.transaction_value_corrections import _validate_proposed
        for field in ['amount', 'actual_gross_amount', 'transaction_cost', 'guarantee_utilized_amount',
                      'guarantee_fee_cash_pct', 'issue_price', 'net_amount', 'confirmed_cash_effect_amount']:
            self.assertIsNone(_validate_proposed(field, None))
            self.assertEqual(_validate_proposed(field, 0), 0)
            with self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed(field, None, allow_unknown=False)

    def test_unknown_overlay_applies_and_reverts_without_editing_original_or_report(self):
        from capital_radar.transaction_feed import _build_transaction_items
        with self.db.connect() as connection:
            initial = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            def propose(value, previous):
                created = create_transaction_value_correction(connection, {
                    'transaction_id': initial['id'], 'issuer_id': self.issuer_id,
                    'field_name': 'guarantee_utilized_amount', 'expected_previous_value': previous,
                    'proposed_value': value, 'source_document_id': self.document_id,
                    'source_excerpt': self.source, 'confidence': .99,
                    'rationale': 'Källan styrker inget garantiutnyttjande för det separata instrumentet.'}, actor='review-agent')
                approve_transaction_value_correction(connection, created['id'], actor='review-agent')
                applied = apply_transaction_value_correction(connection, created['id'], actor='review-agent')
                self.assertEqual(applied['status'], 'applied')
                return created['id']
            wrong = propose(2_400_000, None)
            previous = get_transaction_detail(connection, initial['id'])['effective_transaction_values']['guarantee_utilized_amount']
            clearing = propose(None, previous)
            card = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            self.assertIsNone(card['effective_transaction_values']['guarantee_utilized_amount'])
            fact = card['details']['financing_dossier']['round_reconciliation']['actual_outcome']['guarantee_utilized_amount']
            self.assertIsNone(fact['value'])
            self.assertEqual(fact['basis'], 'audited_effective_overlay')
            self.assertEqual(card['original_transaction_values'], initial['original_transaction_values'])
            self.assertEqual(card['details']['confirmed_cash_effect_amount'], initial['details']['confirmed_cash_effect_amount'])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_events').fetchone()[0], 0)
            self.assertEqual(current_analysis(connection, [self.issuer_id])[0]['reported_runway_months'], 10)
            self.assertEqual(connection.execute('SELECT extracted_text FROM document_contents WHERE document_id=?',
                                              (self.document_id,)).fetchone()[0], self.source)
            revert_transaction_value_correction(connection, clearing, actor='review-agent')
            restored = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            self.assertEqual(restored['effective_transaction_values']['guarantee_utilized_amount'], 2_400_000)
            revert_transaction_value_correction(connection, wrong, actor='review-agent')
    def test_oversubscription_demand_keeps_fraction_units_and_other_caps(self):
        from capital_radar.transaction_value_corrections import _validate_proposed

        for field in ("final_subscription_pct", "subscription_without_rights_pct",
                      "subscription_without_guarantees_pct"):
            for value in (0, .196, 1, 1.58, 3.38, 100):
                with self.subTest(field=field, value=value):
                    self.assertEqual(_validate_proposed(field, value), float(value))
            for value in (-.01, 100.01, True, float("nan"), float("inf")):
                with self.subTest(field=field, invalid=value), self.assertRaises(TransactionValueCorrectionError):
                    _validate_proposed(field, value)
        for field in ("subscription_with_rights_pct", "subscription_commitment_pct",
                      "guarantee_commitment_pct", "guarantee_utilized_pct",
                      "guarantee_fee_pct", "guarantee_fee_cash_pct",
                      "guarantee_fee_share_pct", "transaction_cost_pct"):
            self.assertEqual(_validate_proposed(field, .10), .10)
            with self.subTest(bounded_field=field), self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed(field, 1.01)

    def test_source_backed_oversubscription_applies_and_reverts_without_new_cash(self):
        source = (self.source + " Teckningsanmälningar utan teckningsrätter motsvarar 158 procent. "
                  "Teckning utan garantier motsvarar 240 procent av erbjudandet.")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE document_contents SET extracted_text=?,text_sha256=?,char_count=? WHERE document_id=?",
                (source, hashlib.sha256(source.encode()).hexdigest(), len(source), self.document_id),
            )
            transaction_id = current_analysis(connection, [self.issuer_id])[0]["events"][0]["transaction_id"]
            initial = get_transaction_detail(connection, transaction_id)
            event_count = connection.execute("SELECT COUNT(*) FROM financing_events").fetchone()[0]
            for field, value in (("subscription_without_rights_pct", 1.58),
                                 ("subscription_without_guarantees_pct", 2.4)):
                before = get_transaction_detail(connection, transaction_id)
                created = create_transaction_value_correction(connection, {
                    "transaction_id": transaction_id, "issuer_id": self.issuer_id,
                    "field_name": field,
                    "expected_previous_value": before["effective_transaction_values"][field],
                    "proposed_value": value, "source_document_id": self.document_id,
                    "source_excerpt": source, "confidence": .99,
                    "rationale": "Källstyrkt teckningsefterfrågan i andelar; ingen mottagen kontantlikvid.",
                }, actor="review-agent")
                approve_transaction_value_correction(connection, created["id"], actor="review-agent")
                apply_transaction_value_correction(connection, created["id"], actor="review-agent")
                after = get_transaction_detail(connection, transaction_id)
                self.assertEqual(after["effective_transaction_values"][field], value)
                self.assertEqual(after["original_transaction_values"], initial["original_transaction_values"])
                self.assertEqual(after["source_document_ids"], initial["source_document_ids"])
                self.assertEqual(after["amount"], initial["amount"])
                self.assertEqual(after["details"]["confirmed_cash_effect_amount"],
                                 initial["details"]["confirmed_cash_effect_amount"])
                self.assertEqual(current_analysis(connection, [self.issuer_id])[0]["reported_runway_months"], 10)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM financing_events").fetchone()[0], event_count)
                self.assertEqual(connection.execute(
                    "SELECT extracted_text FROM document_contents WHERE document_id=?", (self.document_id,)
                ).fetchone()[0], source)
                revert_transaction_value_correction(connection, created["id"], actor="review-agent")
                self.assertEqual(get_transaction_detail(connection, transaction_id)["effective_transaction_values"][field],
                                 before["effective_transaction_values"][field])

    def setUp(self) -> None:
        Path(".tmp-tests").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=".tmp-tests")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "values.db")
        self.db.initialize()
        self.source = "Emissionen uppgår till 36 MSEK före kostnader."
        self.db.upsert_document(DocumentCandidate(
            source_id="mfn", external_id="realfiction-round",
            issuer_name="Realfiction AB",
            title="Realfiction avser genomföra företrädesemission om 30 MSEK",
            canonical_url="https://mfn.se/a/realfiction/round",
            published_at="2026-09-20T08:00:00Z", language="sv",
        ), universe_only=False)
        with self.db.connect() as connection:
            document = connection.execute(
                "SELECT id,issuer_id FROM documents ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.document_id = int(document["id"])
            self.issuer_id = int(document["issuer_id"])
            connection.execute(
                """INSERT INTO document_contents(document_id,source_url,extracted_text,
                       text_sha256,char_count,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'extracted','2026-09-20','2026-09-20')""",
                (self.document_id, "https://mfn.se/a/realfiction/round", self.source,
                 hashlib.sha256(self.source.encode()).hexdigest(), len(self.source)),
            )
            values = dict(
                issuer_id=self.issuer_id, as_of_date="2026-09-20",
                report_document_id=self.document_id, report_period_end="2026-06-30",
                currency="SEK", baseline_cash=10_000_000,
                operating_cash_flow=-3_000_000, period_months=3,
                monthly_operating_burn=1_000_000, confirmed_post_report_adjustment=0,
                committed_liquidity=0, conditional_liquidity=0,
                adjusted_cash=10_000_000, weighted_liquidity=0,
                operating_runway_months=10, adjusted_runway_months=10,
                operating_need_score=50, financing_adjustment_points=0,
                adjusted_need_score=50, need_timing="6-12_months",
                alert_level="medium", evidence_posture="fixture",
                calculation_json="{}", calculation_sha256="fixture-value-correction",
                created_at="2026-09-20T10:00:00Z",
            )
            connection.execute(
                "INSERT INTO runway_assessments (" + ",".join(values) + ") VALUES (" +
                ",".join("?" for _ in values) + ")",
                tuple(values.values()),
            )

    def test_review_apply_recalculate_and_revert_preserve_originals(self) -> None:
        with self.db.connect() as connection:
            original = current_analysis(connection, [self.issuer_id])[0]
            transaction_id = original["events"][0]["transaction_id"]
            card = get_transaction_detail(connection, transaction_id)
            previous = card["amount"]
            proposal = {
                "transaction_id": transaction_id,
                "issuer_id": self.issuer_id,
                "field_name": "amount",
                "expected_previous_value": previous,
                "proposed_value": 36_000_000,
                "source_document_id": self.document_id,
                "source_excerpt": self.source,
                "confidence": 0.97,
                "rationale": "Källan anger emissionens totalbelopp, inte teckningsavsikten.",
            }
            with self.assertRaises(TransactionValueCorrectionError):
                create_transaction_value_correction(
                    connection, {**proposal, "source_excerpt": "Påhittat emissionsbelopp"},
                    actor="review-agent",
                )
            created = create_transaction_value_correction(connection, proposal, actor="review-agent")
            correction_id = int(created["id"])
            self.assertEqual(created["status"], "proposed")
            with self.assertRaises(TransactionValueCorrectionError):
                apply_transaction_value_correction(connection, correction_id, actor="review-agent")
            approve_transaction_value_correction(connection, correction_id, actor="human-reviewer")
            applied = apply_transaction_value_correction(connection, correction_id, actor="human-reviewer")
            self.assertEqual(applied["status"], "applied")
            corrected = get_transaction_detail(connection, transaction_id)
            self.assertEqual(corrected["amount"], 36_000_000)
            self.assertEqual(corrected["applied_transaction_correction_ids"], [correction_id])
            self.assertEqual(corrected["original_transaction_values"]["amount"], previous)
            self.assertEqual(corrected["effective_transaction_values"]["amount"], 36_000_000)
            self.assertEqual(corrected["details"]["announced_amount"], 36_000_000)
            self.assertIn("36 000 000", corrected["summary"])
            self.assertEqual(current_analysis(connection, [self.issuer_id])[0]["reported_runway_months"], 10)
            audit = connection.execute("SELECT * FROM adjusted_runway_recalculations").fetchone()
            self.assertIsNotNone(audit)
            self.assertEqual(len(str(audit["calculation_hash"])), 64)
            detail = transaction_value_correction_detail(connection, correction_id)
            self.assertEqual(detail["original_transaction_values"]["amount"], previous)
            self.assertEqual(detail["applied_transaction_correction_ids"], [correction_id])
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM financing_events"
            ).fetchone()[0], 0)
            original_assessment = connection.execute(
                "SELECT operating_runway_months,adjusted_runway_months FROM runway_assessments"
            ).fetchone()
            self.assertEqual(tuple(original_assessment), (10, 10))
            reverted = revert_transaction_value_correction(connection, correction_id, actor="human-reviewer")
            self.assertEqual(reverted["status"], "reverted")
            self.assertEqual(get_transaction_detail(connection, transaction_id)["amount"], previous)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM adjusted_runway_recalculations"
            ).fetchone()[0], 2)

    def test_audited_instrument_keeps_originals_and_reverts_without_cash(self) -> None:
        from capital_radar.transaction_feed import _build_transaction_items
        with self.db.connect() as connection:
            parent = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            instrument = {'canonical_round_key': 'separate-directed-2026', 'category': 'directed_issue',
                          'status': 'approved', 'lifecycle_stage': 'announced', 'amount': 20_000_000,
                          'amount_basis': 'announced_gross', 'currency': 'SEK',
                          'confirmed_cash_effect_amount': 0, 'source_document_ids': [self.document_id]}
            created = create_transaction_value_correction(connection, {
                'transaction_id': parent['id'], 'issuer_id': self.issuer_id,
                'field_name': 'additional_instruments', 'expected_previous_value': None,
                'proposed_value': [instrument], 'source_document_id': self.document_id,
                'source_excerpt': self.source, 'confidence': .99, 'rationale': 'Separat juridiskt instrument, inte sammanlagd likvid.'}, actor='review-agent')
            approve_transaction_value_correction(connection, created['id'], actor='review-agent')
            applied = apply_transaction_value_correction(connection, created['id'], actor='review-agent')
            self.assertEqual(applied['status'], 'applied')
            cards = _build_transaction_items(connection, issuer_ids=[self.issuer_id])
            self.assertEqual(len(cards), 2)
            child = next(c for c in cards if c.get('projection_parent_id'))
            self.assertEqual(child['amount'], 20_000_000)
            self.assertEqual(child['category'], 'directed_issue')
            self.assertEqual(child['details']['confirmed_cash_effect_amount'], 0)
            self.assertIsNone(child['details']['financing_dossier'].get('transaction_cost'))
            self.assertEqual(child['applied_transaction_correction_ids'], [created['id']])
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_events').fetchone()[0], 0)
            self.assertEqual(current_analysis(connection, [self.issuer_id])[0]['reported_runway_months'], 10)
            revert_transaction_value_correction(connection, created['id'], actor='review-agent')
            self.assertEqual(len(_build_transaction_items(connection, issuer_ids=[self.issuer_id])), 1)

    def test_structured_projections_reject_unverified_scope_and_cash(self) -> None:
        from capital_radar.transaction_value_corrections import _validate_proposed, validate_projection_scope
        instrument = {'canonical_round_key': 'new-round', 'category': 'directed_issue', 'status': 'approved',
                      'lifecycle_stage': 'announced', 'amount': 20_000_000, 'amount_basis': 'announced_gross',
                      'currency': 'SEK', 'confirmed_cash_effect_amount': 0, 'source_document_ids': [self.document_id]}
        for invalid in [[{**instrument, 'confirmed_cash_effect_amount': 20_000_000}],
                        [instrument, instrument], [{**instrument, 'source_document_ids': [True]}],
                        [{**instrument, 'amount': -1}], [{**instrument, 'unexpected': 1}]]:
            with self.subTest(invalid=invalid), self.assertRaises(TransactionValueCorrectionError):
                _validate_proposed('additional_instruments', invalid)
        with self.assertRaises(TransactionValueCorrectionError):
            validate_projection_scope('additional_instruments', [instrument], {}, self.document_id + 1)

    def test_unkeyed_raw_instrument_blocks_apply_before_recalculation(self):
        import copy
        from unittest.mock import patch
        from capital_radar.transaction_feed import _build_transaction_items

        with self.db.connect() as connection:
            parent = _build_transaction_items(connection, issuer_ids=[self.issuer_id])[0]
            instrument = {'canonical_round_key': 'hibox-consideration-fixture',
                          'category': 'directed_issue', 'status': 'conditional',
                          'lifecycle_stage': 'announced', 'amount': 13_000_000,
                          'amount_basis': 'announced_gross', 'currency': 'SEK',
                          'confirmed_cash_effect_amount': 0,
                          'source_document_ids': [self.document_id]}
            created = create_transaction_value_correction(connection, {
                'transaction_id': parent['id'], 'issuer_id': self.issuer_id,
                'field_name': 'additional_instruments', 'expected_previous_value': None,
                'proposed_value': [instrument], 'source_document_id': self.document_id,
                'source_excerpt': self.source, 'confidence': .99,
                'rationale': 'Separat säljarvederlag; inte kontant finansieringsinflöde.'}, actor='review-agent')
            approve_transaction_value_correction(connection, created['id'], actor='review-agent')

            def with_raw_candidate(*args, **kwargs):
                cards = _build_transaction_items(*args, **kwargs)
                children = [c for c in cards if c.get('projection_parent_id')]
                if children:
                    raw = copy.deepcopy(children[0])
                    raw['id'] = 'transaction:fixture:directed_issue:raw'
                    raw.pop('projection_parent_id')
                    raw.pop('projection_origin')
                    raw['effective_transaction_values']['canonical_round_key'] = None
                    cards.append(raw)
                return cards

            with patch('capital_radar.transaction_feed._build_transaction_items', side_effect=with_raw_candidate), \
                    patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
                applied = apply_transaction_value_correction(connection, created['id'], actor='review-agent')
                self.assertEqual(applied['status'], 'failed')
                self.assertEqual(applied['last_apply_error'], 'additional_instrument_projection_ambiguous')
                recalculate.assert_not_called()
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM financing_transaction_value_overrides WHERE active=1'
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM adjusted_runway_recalculations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM financing_events').fetchone()[0], 0)
            after = _build_transaction_items(connection, issuer_ids=[self.issuer_id])
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0]['original_transaction_values'], parent['original_transaction_values'])
            self.assertEqual(connection.execute(
                'SELECT extracted_text FROM document_contents WHERE document_id=?', (self.document_id,)
            ).fetchone()[0], self.source)
            self.assertEqual(tuple(connection.execute(
                'SELECT operating_runway_months,adjusted_runway_months FROM runway_assessments'
            ).fetchone()), (10, 10))

    def test_instrument_cardinality_checks_preserve_distinct_instruments(self):
        import copy
        from capital_radar.transaction_value_corrections import additional_instrument_projection_matches
        instrument = {'canonical_round_key': 'separate-fixture', 'category': 'directed_issue',
                      'status': 'conditional', 'lifecycle_stage': 'announced', 'amount': 13_000_000.0,
                      'amount_basis': 'announced_gross', 'currency': 'SEK',
                      'confirmed_cash_effect_amount': 0.0, 'source_document_ids': [1]}
        child = {'id': 'child', 'projection_parent_id': 'parent',
                 'projection_origin': 'audited_separate_instrument', 'source_document_ids': [1],
                 'effective_transaction_values': {k: v for k, v in instrument.items() if k != 'source_document_ids'}}
        check = lambda feed: additional_instrument_projection_matches('parent', [instrument], feed)
        self.assertTrue(check([child]))
        self.assertFalse(check([]))
        self.assertFalse(check([child, copy.deepcopy(child)]))
        raw = copy.deepcopy(child)
        raw.pop('projection_parent_id')
        raw.pop('projection_origin')
        raw['effective_transaction_values']['canonical_round_key'] = None
        self.assertFalse(check([child, raw]))
        for field, value in [('category', 'rights_issue'), ('amount', 19_400_000.0), ('currency', 'EUR')]:
            other = copy.deepcopy(raw)
            other['effective_transaction_values'][field] = value
            self.assertTrue(check([child, other]))
        other = copy.deepcopy(raw)
        other['source_document_ids'] = [2]
        self.assertTrue(check([child, other]))

    def test_effective_stages_preserve_original_stage_inventory(self) -> None:
        from capital_radar.transaction_feed import _project_transaction_values
        from copy import deepcopy
        original = [{'lifecycle_stage': 'registered', 'effective_at': '2026-09-25',
                     'source_document_ids': [self.document_id], 'reported_values': {'gross_amount': 445_000_000}}]
        corrected = [{'lifecycle_stage': 'settled', 'effective_at': '2026-09-25',
                      'source_document_ids': [self.document_id], 'reported_values': {'gross_amount': 445_000}}]
        card = {'id': 'transaction:1:warrant_exercise:1', 'issuer_name': 'Example', 'category': 'warrant_exercise',
                'status': 'completed', 'amount': 445_000, 'amount_basis': 'outcome_gross', 'currency': 'SEK',
                'source_document_ids': [self.document_id], 'published_at': '2026-09-25',
                'details': {'financing_dossier': {'round_reconciliation': {'stages': deepcopy(original)}}}}
        _project_transaction_values(card, {'values': {'effective_stages': corrected}, 'correction_ids': [42]})
        reconciliation = card['details']['financing_dossier']['round_reconciliation']
        self.assertEqual(reconciliation['stages'], corrected)
        self.assertEqual(reconciliation['original_stages'], original)
        self.assertEqual(card['original_transaction_values']['effective_stages'], original)

    def test_failed_recalculation_rolls_back_override_and_persists_failure(self) -> None:
        with self.db.connect() as connection:
            transaction_id = current_analysis(connection, [self.issuer_id])[0]["events"][0]["transaction_id"]
            previous = get_transaction_detail(connection, transaction_id)["amount"]
            created = create_transaction_value_correction(connection, {
                "transaction_id": transaction_id,
                "issuer_id": self.issuer_id,
                "field_name": "amount",
                "expected_previous_value": previous,
                "proposed_value": 36_000_000,
                "source_document_id": self.document_id,
                "source_excerpt": self.source,
                "confidence": 0.97,
                "rationale": "Verifierar atomisk rollback om runway-beräkningen misslyckas.",
            }, actor="review-agent")
            correction_id = int(created["id"])
            approve_transaction_value_correction(connection, correction_id, actor="human-reviewer")
            with patch(
                "capital_radar.radar_analysis.recalculate_adjusted_runway",
                side_effect=RuntimeError("forced recalculation failure"),
            ):
                failed = apply_transaction_value_correction(
                    connection, correction_id, actor="human-reviewer"
                )
            self.assertEqual(failed["status"], "failed")
            self.assertIn("forced recalculation failure", failed["last_apply_error"])
            self.assertEqual(get_transaction_detail(connection, transaction_id)["amount"], previous)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM financing_transaction_value_overrides WHERE active=1"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM adjusted_runway_recalculations"
            ).fetchone()[0], 0)

    def test_move_preserves_other_round_link_and_non_lifo_revert_is_blocked(self) -> None:
        with self.db.connect() as connection:
            transaction_id = current_analysis(
                connection, [self.issuer_id]
            )[0]["events"][0]["transaction_id"]
            card = get_transaction_detail(connection, transaction_id)

            def relation_proposal(
                *, action: str, round_key: str, target_transaction: str,
                expected_previous_value: list[dict], rationale: str,
            ) -> dict:
                return {
                    "transaction_id": transaction_id,
                    "issuer_id": self.issuer_id,
                    "field_name": "document_relation",
                    "expected_previous_value": expected_previous_value,
                    "proposed_value": {
                        "document_id": self.document_id,
                        "canonical_round_key": round_key,
                        "category": card["category"],
                        "action": action,
                        "transaction_id": target_transaction,
                    },
                    "source_document_id": self.document_id,
                    "source_excerpt": self.source,
                    "confidence": 0.98,
                    "rationale": rationale,
                }

            copy = create_transaction_value_correction(
                connection,
                relation_proposal(
                    action="copy", round_key="realfiction_round_b",
                    target_transaction="transaction:1:rights_issue:round-b",
                    expected_previous_value=[],
                    rationale="Samma dokument beskriver ett separat instrument i runda B.",
                ),
                actor="autonomous-agent",
            )
            approve_transaction_value_correction(
                connection, int(copy["id"]), actor="autonomous-agent"
            )
            self.assertEqual(
                apply_transaction_value_correction(
                    connection, int(copy["id"]), actor="autonomous-agent"
                )["status"],
                "applied",
            )
            current_links = [dict(row) for row in connection.execute(
                """SELECT canonical_round_key,category,financing_event_id,transaction_id
                   FROM financing_transaction_document_links
                   WHERE document_id=? AND active=1 ORDER BY id""",
                (self.document_id,),
            ).fetchall()]
            self.assertEqual(len(current_links), 2)

            move = create_transaction_value_correction(
                connection,
                relation_proposal(
                    action="move", round_key="realfiction_round_c",
                    target_transaction="transaction:1:rights_issue:round-c",
                    expected_previous_value=current_links,
                    rationale="Flyttar endast den valda källrelationen till runda C.",
                ),
                actor="autonomous-agent",
            )
            approve_transaction_value_correction(
                connection, int(move["id"]), actor="autonomous-agent"
            )
            self.assertEqual(
                apply_transaction_value_correction(
                    connection, int(move["id"]), actor="autonomous-agent"
                )["status"],
                "applied",
            )
            active = connection.execute(
                """SELECT canonical_round_key FROM financing_transaction_document_links
                   WHERE document_id=? AND active=1 ORDER BY canonical_round_key""",
                (self.document_id,),
            ).fetchall()
            self.assertEqual(
                [row["canonical_round_key"] for row in active],
                ["realfiction_round_b", "realfiction_round_c"],
            )
            with self.assertRaisesRegex(
                TransactionValueCorrectionError,
                "senare aktiv rättning",
            ):
                revert_transaction_value_correction(
                    connection, int(copy["id"]), actor="autonomous-agent"
                )


if __name__ == "__main__":
    unittest.main()
