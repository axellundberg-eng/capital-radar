"""Splits must retain source-bound scalar corrections even if the anchor moves."""
import unittest
from unittest.mock import patch

import test_transaction_feed as fixtures
from capital_radar.transaction_feed import get_transaction_feed
from capital_radar.transaction_corrections import (
    create_transaction_correction, approve_transaction_correction,
    apply_transaction_correction, revert_transaction_correction,
    transaction_correction_detail,
    TransactionCorrectionError,
)
from capital_radar.projection_revision import read_projection_revision
from capital_radar.transaction_value_corrections import (
    create_transaction_value_correction, approve_transaction_value_correction,
    apply_transaction_value_correction, transaction_value_correction_detail,
)


class SplitScalarIdentityTests(unittest.TestCase):
    setUp = fixtures.TransactionFeedTests.setUp
    add = fixtures.TransactionFeedTests.add
    add_event = fixtures.TransactionFeedTests.add_event

    def _case(self):
        # Production IDs21609/10 were imported before the main-round sources.
        # Their publication dates are later. ID order is not lifecycle order.
        rows = [
            ('Vivesto carries out a directed share issue to guarantors in connection with the completed rights issue', '2026-01-29', 'en', True),
            ('Vivesto genomför riktad nyemission till garanter i samband med den genomförda företrädesemissionen', '2026-01-29', 'sv', True),
            ('Vivesto announces outcome of the rights issue', '2026-01-23', 'en', False),
            ('Vivesto offentliggör utfall i företrädesemissionen', '2026-01-23', 'sv', False),
            ('Vivesto publishes information document regarding the rights issue', '2026-01-02', 'en', False),
            ('Vivesto offentliggör informationsdokument avseende företrädesemissionen', '2026-01-02', 'sv', False),
            ('Vivesto resolves on a fully secured rights issue', '2025-11-20', 'en', False),
            ('Vivesto beslutar om en fullt säkerställd företrädesemission', '2025-11-20', 'sv', False),
        ]
        main, fee = [], []
        excerpt = 'Subscription commitments amount to SEK 14.8 million, or 27.5 percent of the rights issue.'
        for title, date, language, compensation in rows:
            text = ('The rights issue was resolved on 20 November 2025. '
                    'Företrädesemissionen beslutades den 20 november 2025. '
                    'The rights issue amounts to SEK 53.8 million. ' + excerpt)
            if compensation:
                text += ' The directed share issue amounts to SEK 3,105,000. Payment is by set-off of guarantee commission claims.'
            doc, _ = self.add(title, body=text, date=date+'T08:00:00Z', issuer='Vivesto AB', language=language)
            (fee if compensation else main).append(doc)
            self.add_event(doc, round_key='raw-rights-round', stage='outcome_published',
                           gross_amount=53_800_000, guarantee_commitment_amount=39_000_000,
                           liquidity_effect_kind='conditional_liquidity')
        return main, fee, excerpt

    def _scalar(self, c, owner, field, value, source, excerpt):
        current = next(i for i in get_transaction_feed(c)['items'] if i['id'] == owner['id'])
        correction = create_transaction_value_correction(c, {
            'transaction_id': owner['id'], 'issuer_id': owner['issuer_id'], 'field_name': field,
            'expected_previous_value': current['effective_transaction_values'][field],
            'proposed_value': value, 'source_document_id': source, 'source_excerpt': excerpt,
            'confidence': .99, 'rationale': 'Preserve the source-bound effective value and its owner.'}, actor='fixture')
        approve_transaction_value_correction(c, correction['id'], actor='fixture')
        apply_transaction_value_correction(c, correction['id'], actor='fixture')
        return correction['id']

    def _split(self, c, owner, main, fee):
        correction = create_transaction_correction(c, {
            'correction_type': 'split_transaction', 'transaction_id': owner['id'], 'issuer_id': owner['issuer_id'],
            'source_transaction_document_ids': sorted(main+fee), 'resulting_rounds': [
                {'canonical_round_key':'vivesto-rights-issue-2025-2026','document_ids':main},
                {'canonical_round_key':'vivesto-guarantee-compensation-2026-01','document_ids':fee}],
            'reason':'Keep two legal instruments without losing earlier reviewed values.'}, actor='fixture')
        approve_transaction_correction(c, correction['id'], actor='fixture')
        return correction['id']

    def _reject_without_side_effects(self, c, correction, code):
        before = get_transaction_feed(c)['items']
        revision = read_projection_revision(c)
        originals = {table: [tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')]
                     for table in ['documents','document_contents','financing_events','financing_event_evidence',
                                   'financing_transaction_value_corrections','financing_transaction_value_overrides']}
        with patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
            with self.assertRaisesRegex(TransactionCorrectionError, code):
                apply_transaction_correction(c, correction, actor='fixture')
            recalculate.assert_not_called()
        self.assertEqual(get_transaction_feed(c)['items'], before)
        self.assertEqual(read_projection_revision(c), revision)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_document_overrides WHERE active=1').fetchone()[0], 0)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM financing_transaction_projection_bindings WHERE active=1').fetchone()[0], 0)
        for table, rows in originals.items():
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')], rows)

    def test_lower_id_compensation_cannot_drop_rights_scalar_ownership(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            before = get_transaction_feed(c)['items']
            rights = next(i for i in before if i['category'] == 'rights_issue')
            old_id = rights['id']
            self.assertTrue(old_id.endswith(':'+str(fee[0])))
            overlays = []
            for field, value in [('subscription_commitment_amount', 14_800_000), ('subscription_commitment_pct', .275)]:
                current = next(i for i in get_transaction_feed(c)['items'] if i['id'] == old_id)
                correction = create_transaction_value_correction(c, {
                    'transaction_id': old_id, 'issuer_id': rights['issuer_id'], 'field_name': field,
                    'expected_previous_value': current['effective_transaction_values'][field],
                    'proposed_value': value, 'source_document_id': main[-1], 'source_excerpt': excerpt,
                    'confidence': .99, 'rationale': 'Source-bound subscription commitment, not received cash.'}, actor='fixture')
                approve_transaction_value_correction(c, correction['id'], actor='fixture')
                apply_transaction_value_correction(c, correction['id'], actor='fixture')
                overlays.append(correction['id'])
            original_values = [tuple(r) for r in c.execute('SELECT * FROM financing_transaction_value_corrections ORDER BY id')]
            before_split = get_transaction_feed(c)['items']
            original_sources = {table: [tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')]
                                for table in ['documents','document_contents','financing_events','financing_event_evidence']}
            split = create_transaction_correction(c, {
                'correction_type': 'split_transaction', 'transaction_id': old_id, 'issuer_id': rights['issuer_id'],
                'source_transaction_document_ids': sorted(main+fee), 'resulting_rounds': [
                    {'canonical_round_key':'vivesto-rights-issue-2025-2026','document_ids':main},
                    {'canonical_round_key':'vivesto-guarantee-compensation-2026-01','document_ids':fee}],
                'reason':'Separate noncash guarantor compensation from the original rights issue.'}, actor='fixture')
            approve_transaction_correction(c, split['id'], actor='fixture')
            apply_transaction_correction(c, split['id'], actor='fixture')
            result = get_transaction_feed(c)['items']
            self.assertEqual(len(result), 2)
            rights_after = next(i for i in result if i['category'] == 'rights_issue')
            self.assertEqual(rights_after['source_document_ids'], sorted(main))
            with self.subTest(scope='stable_identity'):
                self.assertEqual(rights_after['id'], old_id)
            with self.subTest(scope='scalar_provenance'):
                self.assertTrue(set(overlays).issubset(rights_after['applied_transaction_correction_ids']))
            self.assertEqual(rights_after['effective_transaction_values']['subscription_commitment_amount'], 14_800_000)
            self.assertEqual(rights_after['effective_transaction_values']['subscription_commitment_pct'], .275)
            self.assertTrue(transaction_correction_detail(c, split['id'])['current_round_matches_applied'])
            for correction in overlays:
                detail = transaction_value_correction_detail(c, correction)
                self.assertIn(correction, detail['applied_transaction_correction_ids'])
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM financing_transaction_value_corrections ORDER BY id')], original_values)
            revert_transaction_correction(c, split['id'], actor='fixture')
            self.assertEqual(get_transaction_feed(c)['items'], before_split)
            for table, rows in original_sources.items():
                self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')], rows)

    def test_explicit_null_remains_owned_without_becoming_zero(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            correction = self._scalar(c, owner, 'guarantee_commitment_amount', None, main[-1], excerpt)
            apply_transaction_correction(c, self._split(c, owner, main, fee), actor='fixture')
            current = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self.assertIsNone(current['effective_transaction_values']['guarantee_commitment_amount'])
            self.assertIn(correction, current['applied_transaction_correction_ids'])

    def test_structured_stages_are_not_implicitly_reassigned(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'effective_stages', [{'lifecycle_stage':'announced',
                'effective_at':'2025-11-20','source_document_ids':[main[-1]],
                'reported_values':{'gross_amount':53_800_000}}], main[-1], excerpt)
            self._reject_without_side_effects(c, self._split(c, owner, main, fee), 'grouping_split_structured_value_override')

    def test_structured_additional_security_cannot_be_hidden_by_split(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'additional_instruments', [{'canonical_round_key':'independent-warrants',
                'category':'warrant_exercise','status':'announced','lifecycle_stage':'announced',
                'amount':1_000_000,'amount_basis':'maximum_proceeds','currency':'SEK',
                'confirmed_cash_effect_amount':0,'source_document_ids':[main[-1]]}],main[-1],excerpt)
            self._reject_without_side_effects(c, self._split(c, owner, main, fee), 'grouping_split_structured_value_override')

    def test_scalar_sources_in_different_legal_rounds_are_rejected_atomically(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, main[-1], excerpt)
            self._scalar(c, owner, 'subscription_commitment_pct', .275, fee[0], excerpt)
            self._reject_without_side_effects(c, self._split(c, owner, main, fee), 'grouping_split_value_source_conflict')

    def test_stable_compensation_currency_and_moving_rights_values_both_survive(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            cards = get_transaction_feed(c)['items']
            owner = next(i for i in cards if i['category']=='rights_issue')
            other = next(i for i in cards if i['category']=='directed_issue')
            main_scalar = self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, main[-1], excerpt)
            fee_scalar = self._scalar(c, other, 'currency', 'SEK', fee[1], excerpt)
            before = get_transaction_feed(c)['items']
            correction = self._split(c, owner, main, fee)
            apply_transaction_correction(c, correction, actor='fixture')
            after = get_transaction_feed(c)['items']
            rights = next(i for i in after if i['category']=='rights_issue')
            compensation = next(i for i in after if i['category']=='directed_issue')
            self.assertEqual(rights['id'], owner['id'])
            self.assertEqual(compensation['id'], other['id'])
            self.assertIn(main_scalar, rights['applied_transaction_correction_ids'])
            self.assertNotIn(main_scalar, compensation['applied_transaction_correction_ids'])
            self.assertIn(fee_scalar, compensation['applied_transaction_correction_ids'])
            self.assertNotIn(fee_scalar, rights['applied_transaction_correction_ids'])
            self.assertEqual(compensation['effective_transaction_values']['currency'], 'SEK')
            revert_transaction_correction(c, correction, actor='fixture')
            self.assertEqual(get_transaction_feed(c)['items'], before)

    def test_conflicting_scalar_round_key_is_not_reinterpreted(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'canonical_round_key', 'different-reviewed-round', main[-1], excerpt)
            self._reject_without_side_effects(c, self._split(c, owner, main, fee), 'grouping_split_value_identity_conflict')

    def test_source_owned_by_other_instrument_does_not_reclassify_it(self):
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, fee[0], excerpt)
            self._reject_without_side_effects(c, self._split(c, owner, main, fee), 'grouping_value_provenance_mismatch')

    def test_post_projection_loss_rolls_back_binding_before_recalculation(self):
        import capital_radar.transaction_corrections as grouping
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, main[-1], excerpt)
            correction = self._split(c, owner, main, fee)
            original = grouping._affected_feed_items
            def lose_scalar(connection, *args, **kwargs):
                cards = original(connection, *args, **kwargs)
                if grouping.effective_document_rounds(connection, main+fee):
                    for card in cards:
                        card['applied_transaction_correction_ids'] = []
                return cards
            with patch.object(grouping, '_affected_feed_items', side_effect=lose_scalar):
                self._reject_without_side_effects(c, correction, 'grouping_value_provenance_mismatch')

    def test_correction_duplicated_onto_other_instrument_is_rejected(self):
        import capital_radar.transaction_corrections as grouping
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            owner = next(i for i in get_transaction_feed(c)['items'] if i['category']=='rights_issue')
            scalar = self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, main[-1], excerpt)
            correction = self._split(c, owner, main, fee)
            original = grouping._affected_feed_items
            def duplicate_scalar(connection, *args, **kwargs):
                cards = original(connection, *args, **kwargs)
                if grouping.effective_document_rounds(connection, main+fee):
                    next(card for card in cards if card['category']=='directed_issue')['applied_transaction_correction_ids'] = [scalar]
                return cards
            with patch.object(grouping, '_affected_feed_items', side_effect=duplicate_scalar):
                self._reject_without_side_effects(c, correction, 'grouping_value_provenance_mismatch')

    def test_loss_of_only_secondary_owner_rolls_back_primary_binding(self):
        import capital_radar.transaction_corrections as grouping
        main, fee, excerpt = self._case()
        with self.db.connect() as c:
            cards = get_transaction_feed(c)['items']
            owner = next(i for i in cards if i['category']=='rights_issue')
            other = next(i for i in cards if i['category']=='directed_issue')
            self._scalar(c, owner, 'subscription_commitment_amount', 14_800_000, main[-1], excerpt)
            self._scalar(c, other, 'currency', 'SEK', fee[1], excerpt)
            correction = self._split(c, owner, main, fee)
            original = grouping._affected_feed_items
            def lose_secondary(connection, *args, **kwargs):
                cards = original(connection, *args, **kwargs)
                if grouping.effective_document_rounds(connection, main+fee):
                    next(card for card in cards if card['category']=='directed_issue')['applied_transaction_correction_ids'] = []
                return cards
            with patch.object(grouping, '_affected_feed_items', side_effect=lose_secondary):
                self._reject_without_side_effects(c, correction, 'grouping_value_provenance_mismatch')
