from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.models import DocumentCandidate
from capital_radar.transaction_feed import _Candidate, _identity, get_transaction_feed


class InstrumentSeriesIdentityTests(unittest.TestCase):
    def test_singular_guarantor_recipients_require_reviewed_identity(self):
        from capital_radar.transaction_feed import _categories, _transaction_status
        for title in [
            'Q-linea resolves to carry out a directed issue of shares to a guarantor in connection with the completed preferential rights issue',
            'Q-linea genomför riktad nyemission av aktier till garant i samband med den genomförda företrädesemissionen',
            'Company resolves on a directed share issue to the guarantor in connection with the completed rights issue',
            'Bolaget beslutar om riktad nyemission till garanten i samband med den genomförda företrädesemissionen',
        ]:
            with self.subTest(title=title):
                self.assertEqual(set(_categories(title)), {'rights_issue', 'directed_issue'})
                self.assertEqual(_categories(title, reviewed=True), ['directed_issue'])
                self.assertIn(_transaction_status(title, '', 'directed_issue', reviewed=True), {'announced', 'approved'})
        for title in [
            "Company resolves on a directed issue to a guarantor's adviser in connection with the completed rights issue",
            'Company resolves on a directed issue to a guarantor and a new rights issue',
            'Bolaget beslutar om riktad emission till garant och en ny företrädesemission',
        ]:
            with self.subTest(title=title):
                self.assertEqual(set(_categories(title, reviewed=True)), {'rights_issue', 'directed_issue'})

    def test_plain_guarantor_references_need_reviewed_identity(self):
        from capital_radar.transaction_feed import _categories, _transaction_status
        for title in [
            'Vivesto carries out a directed share issue to guarantors in connection with the completed rights issue',
            'Vivesto genomför riktad nyemission till garanter i samband med den genomförda företrädesemissionen',
        ]:
            with self.subTest(title=title):
                self.assertEqual(set(_categories(title)), {'rights_issue','directed_issue'})
                self.assertEqual(_categories(title, reviewed=True), ['directed_issue'])
                self.assertNotEqual(_transaction_status(title, '', 'directed_issue', reviewed=True), 'completed')
        for title in [
            'Company resolves on a directed issue to guarantors and a new rights issue',
            'Bolaget beslutar om riktad emission till garanter och en ny företrädesemission',
            'Company resolves on a directed issue in connection with a rights issue',
        ]:
            self.assertEqual(set(_categories(title, reviewed=True)), {'rights_issue','directed_issue'})

    def test_underwriter_compensation_context_does_not_create_another_rights_issue(self):
        from capital_radar.transaction_feed import _categories, _transaction_status
        titles = [
            "Alzinova’s Board resolves on a directed issue of units to underwriters in connection with the completed rights issue",
            "Alzinovas styrelse beslutar om riktad emission av units till emissionsgarant i samband med den avslutade företrädesemissionen",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(_categories(title), ['directed_issue'])
                self.assertEqual(_transaction_status(title, '', 'directed_issue'), 'approved')
        for title in [
            'Company resolves on a rights issue and a directed issue',
            'Bolaget beslutar om företrädesemission och riktad emission',
        ]:
            self.assertEqual(set(_categories(title)), {'rights_issue', 'directed_issue'})

    def test_english_to_numbers_are_not_instrument_series(self):
        candidate = _Candidate(
            {"title": "Outcome of warrants TO2"}, "warrant_exercise", "completed",
            "Warrants TO2 increase the number of shares to 274,143,719 shares.", [],
        )
        _identity(candidate)
        self.assertEqual(candidate.series, {"TO2"})
        convertible = _Candidate(
            {"title": "Convertible notes"}, "convertible", "announced",
            "The convertibles may be extended up to 30 months and convert at a price "
            "corresponding to 80 percent, increasing the shares to 6,302,631.", [],
        )
        _identity(convertible)
        self.assertEqual(convertible.series, set())

    def test_real_spaced_compact_and_year_series_are_preserved(self):
        for label, expected in [
            ("TO 2 B", {"TO2B"}), ("to2", {"TO2"}),
            ("KV 3", {"KV3"}), ("series 2026/2027", {"2026/2027"}),
        ]:
            with self.subTest(label=label):
                candidate = _Candidate(
                    {"title": "Warrants"}, "warrant_exercise", "announced",
                    f"Warrants of {label} may be exercised.", [],
                )
                _identity(candidate)
                self.assertEqual(candidate.series, expected)


class TransactionFeedTests(unittest.TestCase):
    def test_singular_compensation_split_keeps_both_languages_out_of_main_round(self):
        from capital_radar.transaction_corrections import (
            create_transaction_correction, approve_transaction_correction,
            apply_transaction_correction, transaction_correction_detail,
            revert_transaction_correction, GroupingReadbackMismatch,
        )
        compensation = []
        main = []
        # The smaller IDs belong to compensation, as in the archived Q-linea case.
        titles = [
            ('Q-linea resolves to carry out a directed issue of shares to a guarantor in connection with the completed preferential rights issue', '2025-11-17', 'en'),
            ('Q-linea genomför riktad nyemission av aktier till garant i samband med den genomförda företrädesemissionen', '2025-11-17', 'sv'),
            ('Q-linea announces outcome of rights issue', '2025-11-11', 'en'),
            ('Q-linea offentliggör utfall i företrädesemissionen', '2025-11-11', 'sv'),
            ('Q-linea publishes prospectus in connection with the rights issue', '2025-10-22', 'en'),
            ('Q-linea offentliggör prospekt med anledning av företrädesemission', '2025-10-22', 'sv'),
        ]
        for index, (title, day, language) in enumerate(titles):
            if index < 2:
                body = ('The board resolves on 16,800 compensation shares at SEK 25 per share. '
                        'The guarantee commission claim of SEK 420,000 is paid by set-off. '
                        'The referenced rights issue was resolved on 18 September 2025.')
            else:
                body = ('The rights issue was resolved on 18 September 2025. '
                        'The rights issue amounts to SEK 322 million before costs. '
                        'The subscription price is SEK 25 per share.')
            doc, _ = self.add(title, body=body, date=day+'T13:00:00Z',
                              issuer='Q-linea AB', language=language)
            (compensation if index < 2 else main).append(doc)
            self.add_event(doc, round_key='raw-qlinea-rights', stage='outcome_published',
                           gross_amount=322_000_000, guarantee_commitment_amount=200_000_000,
                           liquidity_effect_kind='conditional_liquidity')
        documents = sorted(main+compensation)
        with self.db.connect() as connection:
            before = get_transaction_feed(connection)['items']
            self.assertEqual(len(before), 2)
            rights = next(c for c in before if c['category']=='rights_issue')
            self.assertEqual(rights['source_document_ids'], documents)
            originals = {table: [tuple(r) for r in connection.execute(f'SELECT * FROM {table} ORDER BY id')]
                         for table in ['documents', 'document_contents', 'financing_events', 'financing_event_evidence', 'runway_assessments']}
            payload = {'correction_type':'split_transaction', 'transaction_id':rights['id'],
                       'issuer_id':rights['issuer_id'], 'source_transaction_document_ids':documents,
                       'resulting_rounds':[
                           {'canonical_round_key':'qlinea-rights-2025-09-18', 'document_ids':main},
                           {'canonical_round_key':'qlinea-guarantee-compensation-2025-11-17', 'document_ids':compensation}],
                       'reason':'Separate the source-reviewed compensation shares from the referenced rights issue.'}
            correction = create_transaction_correction(connection, payload, actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            with patch('capital_radar.transaction_feed._underwriter_compensation_title', return_value=None), \
                    patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
                with self.assertRaises(GroupingReadbackMismatch):
                    apply_transaction_correction(connection, correction['id'], actor='review-agent')
                recalculate.assert_not_called()
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM financing_transaction_document_overrides WHERE active=1'
            ).fetchone()[0], 0)
            apply_transaction_correction(connection, correction['id'], actor='review-agent')
            back = transaction_correction_detail(connection, correction['id'])
            self.assertTrue(back['current_round_matches_applied'])
            after = back['effective_feed_current']
            self.assertEqual(len(after), 2)
            self.assertCountEqual([(c['category'], c['source_document_ids']) for c in after],
                                  [('rights_issue', main), ('directed_issue', compensation)])
            fee = next(c for c in after if c['category']=='directed_issue')
            self.assertEqual(fee['status'], 'approved')
            self.assertEqual(fee['details']['confirmed_cash_effect_amount'], 0)
            self.assertIsNone(fee['details']['financing_dossier']['guarantee_commitment_amount'])
            self.assertNotEqual(fee['amount'], 322_000_000)
            for table, rows in originals.items():
                self.assertEqual([tuple(r) for r in connection.execute(f'SELECT * FROM {table} ORDER BY id')], rows)
            revert_transaction_correction(connection, correction['id'], actor='review-agent')
            restored = get_transaction_feed(connection)['items']
            self.assertCountEqual([(c['id'], c['source_document_ids']) for c in restored],
                                  [(c['id'], c['source_document_ids']) for c in before])

    def test_reviewed_guarantor_compensation_split_keeps_saved_expected_state(self):
        from capital_radar.transaction_corrections import (
            create_transaction_correction, approve_transaction_correction,
            apply_transaction_correction, transaction_correction_detail,
            revert_transaction_correction, GroupingReadbackMismatch,
        )
        main = []
        compensation = []
        titles = [
            ('Vivesto resolves on a fully secured rights issue', '2025-11-20', 'en'),
            ('Vivesto beslutar om en fullt säkerställd företrädesemission', '2025-11-20', 'sv'),
            ('Vivesto publishes information document regarding the rights issue', '2026-01-02', 'en'),
            ('Vivesto offentliggör informationsdokument avseende företrädesemissionen', '2026-01-02', 'sv'),
            ('Vivesto announces outcome of the rights issue', '2026-01-23', 'en'),
            ('Vivesto offentliggör utfall i företrädesemissionen', '2026-01-23', 'sv'),
            ('Vivesto carries out a directed share issue to guarantors in connection with the completed rights issue', '2026-01-29', 'en'),
            ('Vivesto genomför riktad nyemission till garanter i samband med den genomförda företrädesemissionen', '2026-01-29', 'sv'),
        ]
        for index, (title, day, language) in enumerate(titles):
            body = ('The rights issue was resolved on 20 November 2025. '
                    'Företrädesemissionen beslutades den 20 november 2025. '
                    'The rights issue amounts to SEK 53.8 million.')
            if index >= 6:
                body += (' The directed share issue amounts to SEK 3,105,000. '
                         'Payment is by set-off of guarantee commission claims.')
            doc, _ = self.add(title, body=body, date=day+'T08:00:00Z',
                              issuer='Vivesto AB', language=language)
            (compensation if index >= 6 else main).append(doc)
            self.add_event(doc, round_key='raw-rights-round', stage='outcome_published',
                           gross_amount=53_800_000, guarantee_commitment_amount=39_000_000,
                           liquidity_effect_kind='conditional_liquidity')
        documents = sorted(main+compensation)
        with self.db.connect() as connection:
            before = get_transaction_feed(connection)['items']
            rights = next(c for c in before if c['category']=='rights_issue')
            self.assertEqual(rights['source_document_ids'], documents)
            self.assertEqual(len(before), 2)
            originals = {table: [tuple(r) for r in connection.execute(f'SELECT * FROM {table} ORDER BY id')]
                         for table in ['documents', 'document_contents', 'financing_events', 'financing_event_evidence']}
            payload = {'correction_type':'split_transaction', 'transaction_id':rights['id'],
                       'issuer_id':rights['issuer_id'], 'source_transaction_document_ids':documents,
                       'resulting_rounds':[
                           {'canonical_round_key':'vivesto-rights-issue-2025-2026', 'document_ids':main},
                           {'canonical_round_key':'vivesto-guarantee-compensation-2026-01', 'document_ids':compensation}],
                       'reason':'Separate the reviewed set-off compensation from its referenced rights issue.'}
            correction = create_transaction_correction(connection, payload, actor='review-agent')
            approve_transaction_correction(connection, correction['id'], actor='review-agent')
            # Reproduce job607's old classifier, then recover the SAME saved
            # correction after the fix, without changing its expected sources.
            with patch('capital_radar.transaction_feed._underwriter_compensation_title', return_value=None), \
                    patch('capital_radar.radar_analysis.recalculate_adjusted_runway') as recalculate:
                with self.assertRaises(GroupingReadbackMismatch):
                    apply_transaction_correction(connection, correction['id'], actor='review-agent')
                recalculate.assert_not_called()
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM financing_transaction_document_overrides WHERE active=1'
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT status FROM financing_transaction_corrections WHERE id=?', (correction['id'],)
            ).fetchone()[0], 'approved')
            apply_transaction_correction(connection, correction['id'], actor='review-agent')
            back = transaction_correction_detail(connection, correction['id'])
            self.assertTrue(back['current_round_matches_applied'])
            after = back['effective_feed_current']
            self.assertEqual(len(after), 2)
            self.assertCountEqual([(c['category'], c['source_document_ids']) for c in after],
                                  [('rights_issue',main), ('directed_issue',compensation)])
            fee = next(c for c in after if c['category']=='directed_issue')
            self.assertIn(fee['status'], {'announced', 'approved'})
            self.assertEqual(fee['details']['confirmed_cash_effect_amount'], 0)
            self.assertIsNone(fee['details']['financing_dossier']['guarantee_commitment_amount'])
            self.assertEqual(fee['amount'], 3_105_000)
            for table, rows in originals.items():
                self.assertEqual([tuple(r) for r in connection.execute(f'SELECT * FROM {table} ORDER BY id')], rows)
            revert_transaction_correction(connection, correction['id'], actor='review-agent')
            restored = get_transaction_feed(connection)['items']
            self.assertCountEqual([(c['id'],c['source_document_ids']) for c in restored],
                                  [(c['id'],c['source_document_ids']) for c in before])

    def test_compensation_translations_keep_one_directed_issue_and_separate_rights_round(self):
        for title, body, date, language in [
            ('Alzinova announces final outcome of the rights issue',
             'The rights issue was subscribed to 86.2 percent and will provide SEK 44 million before issue costs.',
             '2026-10-07T17:25:00Z', 'en'),
            ('Alzinova’s Board resolves on a directed issue of units to underwriters in connection with the completed rights issue',
             'The directed issue amounts to SEK 3.6 million. Payment is made by set-off of underwriting compensation claims.',
             '2026-10-20T05:30:00Z', 'en'),
            ('Alzinovas styrelse beslutar om riktad emission av units till emissionsgarant i samband med den avslutade företrädesemissionen',
             'Den riktade emissionen uppgår till 3,6 MSEK. Betalning sker genom kvittning av fordran på garantiersättning.',
             '2026-10-20T05:30:00Z', 'sv'),
        ]:
            self.add(title, body=body, date=date, issuer='Alzinova AB', language=language)
        cards = self.feed()['items']
        self.assertEqual(len(cards), 2)
        rights = next(c for c in cards if c['category'] == 'rights_issue')
        compensation = next(c for c in cards if c['category'] == 'directed_issue')
        self.assertEqual(len(rights['source_document_ids']), 1)
        self.assertEqual(len(compensation['source_document_ids']), 2)
        self.assertEqual(compensation['status'], 'approved')
        self.assertEqual(compensation['details']['confirmed_cash_effect_amount'], 0)
    def test_tranche_translations_merge_despite_raw_keys_without_joining_other_drawdowns(self):
        common = "Convertible financing of up to SEK 51.7 million. Konvertibelfinansiering om upp till 51,7 MSEK."
        entries = [
            ("Valuno enters into conditional convertible financing with Helena", "facility", "2026-07-10T17:24:00Z"),
            ("Valuno begär utbetalning av första tranchen under konvertibelfinansieringen med Helena", "facility", "2026-07-16T16:56:00Z"),
            ("Valuno begär utbetalning av andra tranchen under konvertibelfinansieringen med Helena", "facility", "2026-08-31T12:12:00Z"),
            ("Valuno requests drawdown of the third tranche under the Helena convertible financing", "facility", "2026-10-19T11:50:00Z"),
            ("Valuno begär utbetalning av tredje tranchen under konvertibelfinansieringen med Helena", "third-tranche", "2026-10-19T11:50:00Z"),
            ("Valuno requests drawdown of the third tranche under the Orion convertible financing", "orion-third-tranche", "2026-10-19T11:50:00Z"),
        ]
        documents = []
        for title, key, date in entries:
            doc, _ = self.add(title, body=common, date=date, issuer="Valuno Group AB", language="sv")
            documents.append(doc)
            event = self.add_event(doc, round_key=key, stage="announced", financing_type="convertible")
            with self.db.connect() as connection:
                connection.execute("UPDATE financing_event_evidence SET source_excerpt=? WHERE financing_event_id=?", (common, event))
        with self.db.connect() as connection:
            before = [tuple(row) for row in connection.execute("SELECT * FROM financing_events ORDER BY id")]
            connection.execute("PRAGMA query_only=ON")
            result = get_transaction_feed(connection)
            self.assertEqual(before, [tuple(row) for row in connection.execute("SELECT * FROM financing_events ORDER BY id")])
        self.assertEqual(result["total"], 5)
        memberships = {tuple(sorted(item["source_document_ids"])) for item in result["items"]}
        self.assertEqual(memberships, {(documents[0],), (documents[1],), (documents[2],),
                                      (documents[3], documents[4]), (documents[5],)})

    def test_automatic_tranche_translation_cannot_override_audited_separation(self):
        en, _ = self.add("Valuno requests drawdown of the third tranche under the Helena convertible financing",
                         body="Convertible financing of SEK 2.1 million.", issuer="Valuno Group AB", language="en")
        sv, _ = self.add("Valuno begär utbetalning av tredje tranchen under konvertibelfinansieringen med Helena",
                         body="Konvertibelfinansiering om 2,1 MSEK.", issuer="Valuno Group AB")
        with patch("capital_radar.transaction_feed.effective_document_rounds", return_value={en: "audited-a", sv: "audited-b"}):
            self.assertEqual(self.feed()["total"], 2)

    def test_reviewed_round_overrides_series_noise_but_keeps_other_round_separate(self):
        terms, _ = self.add(
            "Villkor för teckningsoptioner TO2",
            body="Teckningsoptioner TO2 kan utnyttjas. Historiska teckningsoptioner TO1 omnämns också.",
        )
        terms_translation, _ = self.add(
            "Terms for warrants TO2",
            body="Warrants TO2 may be exercised. Prior warrants TO1 are also mentioned.",
            language="en",
        )
        outcome, _ = self.add(
            "Outcome of warrants TO2",
            body="Warrants TO2 raised SEK 2.6 million before costs, "
                 "increasing the number of shares to 274,143,719.",
            language="en", date="2026-10-05T08:00:00Z",
        )
        translation, _ = self.add(
            "Utfall av teckningsoptioner TO2",
            body="Teckningsoptioner TO2 tillför bolaget 2,6 MSEK före kostnader.",
            date="2026-10-05T08:00:00Z",
        )
        separate, _ = self.add(
            "Villkor för teckningsoptioner TO3",
            body="Teckningsoptioner TO3 avser ett separat instrument.",
        )
        with patch("capital_radar.transaction_feed.effective_document_rounds", return_value={
            terms: "reviewed-to2", outcome: "reviewed-to2",
            terms_translation: "reviewed-to2",
            translation: "reviewed-to2", separate: "separate-to3",
        }):
            items = self.feed()["items"]
        self.assertEqual(len(items), 2)
        by_key = {item["details"]["financing_dossier"]["canonical_round_key"]: item
                  for item in items}
        self.assertEqual(by_key["reviewed-to2"]["source_document_ids"],
                         [terms, terms_translation, outcome, translation])
        self.assertEqual(by_key["separate-to3"]["source_document_ids"], [separate])

    def test_feed_uses_highest_quality_document_text(self):
        document, url = self.add(
            "Bolaget offentliggör företrädesemission",
            body="Företrädesemission om 10 MSEK.",
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE document_contents SET quality_score=1 WHERE document_id=?",
                (document,),
            )
            connection.execute("""INSERT INTO document_contents
                (document_id,source_url,extracted_text,text_sha256,char_count,
                 quality_score,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'extracted',?,?)""",
                (document, url + "/better", "Företrädesemission om 20 MSEK.",
                 "better", 30, 9, "2026-09-01", "2026-09-01"),
            )
        self.assertEqual(self.feed()["items"][0]["amount"], 20_000_000)

    def test_guarantee_setoff_terms_do_not_change_rights_issue_gross(self):
        terms = "Garanterna kan erhålla garantiersättning kontant om fem (5) procent eller i aktier om tio (10) procent."
        document, _ = self.add("Utfall i företrädesemission om 40 MSEK", body="Företrädesemissionens utfall är 40 MSEK. " + terms)
        self.add_event(document, round_key="rights-2026", stage="outcome_published", gross_amount=40_000_000)
        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        self.assertEqual(dossier["guarantee_fee_details"], terms)
        self.assertEqual(dossier["gross_amount"], 40_000_000)

    def test_credit_limit_is_separate_from_drawn_cash(self):
        document, _ = self.add(
            "Irisity ingår kreditfacilitet om 15 MSEK",
            body="Irisity har avtalat en kreditram om 15 MSEK. Inget belopp har ännu utnyttjats.",
        )
        event_id = self.add_event(document, round_key="irisity-credit-2026", stage="announced",
                                  financing_type="credit_facility", credit_limit=15_000_000,
                                  cash_effect_amount=0, liquidity_effect_kind="committed_liquidity")
        with self.db.connect() as connection:
            connection.execute("UPDATE financing_event_evidence SET source_excerpt=? WHERE financing_event_id=?",
                               ("kreditfacilitet om 15 MSEK", event_id))
        item = self.feed()["items"][0]
        dossier = item["details"]["financing_dossier"]
        self.assertEqual(dossier["credit_limit"], 15_000_000)
        self.assertIsNone(dossier["drawn_amount"])
        self.assertEqual(dossier["confirmed_cash_effect_amount"], 0)
        self.assertEqual(dossier["undrawn_commitment"], 15_000_000)
        self.assertEqual(item["details"]["confirmed_cash_effect_amount"], 0)

    def test_credit_limit_increase_uses_new_limit_not_old_limit_or_increment(self):
        self.add(
            "Irisity utökar befintlig kreditfacilitet ytterligare",
            body=("Utökningen innebär att det maximala beloppet höjs från högst "
                  "10 MSEK till högst 15 MSEK. Tidigare höjdes ramen från 5 MSEK till 10 MSEK."),
        )
        item = self.feed()["items"][0]
        self.assertEqual(item["amount"], 15_000_000)
        self.assertEqual(item["amount_basis"], "credit_limit")
        self.assertEqual(item["details"]["financing_dossier"]["credit_limit"], 15_000_000)
        self.assertEqual(item["details"]["confirmed_cash_effect_amount"], 0)

    def test_credit_draw_counts_only_paid_amount(self):
        document, _ = self.add(
            "Senzime utnyttjar kreditfacilitet om 15 MSEK",
            body="Kreditramen är 15 MSEK varav 4 MSEK har betalats ut.",
        )
        event_id = self.add_event(document, round_key="senzime-credit-2026", stage="settled",
                                  financing_type="credit_facility", credit_limit=15_000_000,
                                  drawn_amount=4_000_000, cash_effect_amount=4_000_000,
                                  liquidity_effect_kind="confirmed_cash")
        with self.db.connect() as connection:
            connection.execute("UPDATE financing_event_evidence SET source_excerpt=? WHERE financing_event_id=?",
                               ("kreditfacilitet om 15 MSEK varav 4 MSEK har betalats ut", event_id))
        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        self.assertEqual(dossier["drawn_amount"], 4_000_000)
        self.assertEqual(dossier["confirmed_cash_effect_amount"], 4_000_000)
        self.assertEqual(dossier["undrawn_commitment"], 11_000_000)

    def test_guarantee_fee_alternatives_are_structured_and_share_fee_is_non_cash(self):
        document, _ = self.add(
            "SciBase offentliggör villkor för företrädesemission",
            body=("Garantiersättningen uppgår till 10 procent kontant eller "
                  "12 procent i aktier av garanterat belopp."),
        )
        self.add_event(document, round_key="scibase-rights-2026", stage="terms_published",
                       guarantee_fee_cash_pct=0.10, guarantee_fee_share_pct=0.12,
                       guarantee_fee_basis="guaranteed_amount")
        options = self.feed()["items"][0]["details"]["financing_dossier"]["guarantee_fee_options"]
        self.assertEqual(options["cash_fee_pct"], 0.10)
        self.assertEqual(options["share_fee_pct"], 0.12)
        self.assertEqual(options["fee_basis"], "guaranteed_amount")
        self.assertEqual(options["share_fee_cash_effect_amount"], 0)

    def test_guarantee_share_fee_ignores_unrelated_share_numbers(self):
        document, _ = self.add(
            "SciBase offentliggör villkor för företrädesemission",
            body=("Aktiekapitalet kan öka med 0,07 procent genom 193 669 aktier. "
                  "Garantiersättningen uppgår till 10 procent kontant alternativt "
                  "12 procent i nyutgivna aktier av garanterat belopp."),
        )
        self.add_event(document, round_key="scibase-rights-safe-fee", stage="terms_published")
        options = self.feed()["items"][0]["details"]["financing_dossier"]["guarantee_fee_options"]
        self.assertEqual(options["cash_fee_pct"], 0.10)
        self.assertEqual(options["share_fee_pct"], 0.12)

    def test_estimated_net_does_not_replace_announced_or_actual_net(self):
        document, _ = self.add(
            "Bolaget beslutar om företrädesemission om 33 MSEK",
            body="Emissionen uppgår till 33 MSEK och emissionskostnaderna beräknas till 5 MSEK.",
        )
        self.add_event(document, round_key="strict-net-2026", stage="announced",
                       gross_amount=33_000_000, transaction_cost=5_000_000)
        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        self.assertIsNone(dossier["announced_net_amount"])
        self.assertIsNone(dossier["actual_net_amount"])
        self.assertEqual(dossier["estimated_net_amount"], 28_000_000)
        self.assertEqual(dossier["net_amount_basis"], "derived_gross_less_source_costs")

    def test_read_snapshot_does_not_poison_shared_feed_cache(self):
        document, _ = self.add("Företrädesemission om 10 MSEK", body="Företrädesemission om 10 MSEK.")
        with self.db.connect() as snapshot:
            snapshot.execute("BEGIN")
            snapshot.execute("SELECT count(*) FROM documents").fetchone()
            with self.db.connect() as writer:
                writer.execute("UPDATE documents SET title='Företrädesemission om 20 MSEK' WHERE id=?", (document,))
                writer.execute("UPDATE document_contents SET extracted_text='Företrädesemission om 20 MSEK.' WHERE document_id=?", (document,))
            self.assertEqual(get_transaction_feed(snapshot)["items"][0]["amount"], 10_000_000)
        self.assertEqual(self.feed()["items"][0]["amount"], 20_000_000)

    def test_main_rights_decision_is_not_overallotment_proposal(self):
        self.add("Styrelsen har beslutat om en företrädesemission om 127 MSEK samt föreslår en övertilldelning om 75 MSEK",
                 body="Styrelsen har beslutat om företrädesemissionen.")
        self.assertEqual(self.feed()["items"][0]["status"], "approved")

    def test_proceeds_headline_with_explicit_final_outcome(self):
        self.add("Teneo AI tillförs 74 MSEK genom företrädesemission",
                 body="Teneo AI har erhållit det slutgiltiga utfallet i företrädesemissionen om 74 MSEK.")
        self.assertEqual(self.feed()["items"][0]["status"], "completed")

    def test_final_outcome_after_page_capture_prefix_is_completed(self):
        self.add(
            "Teneo AI tillförs 74 MSEK genom företrädesemission",
            body=("MFN page capture. " * 400
                  + "Teneo AI har erhållit det slutgiltiga utfallet i företrädesemissionen om 74 MSEK."),
        )
        self.assertEqual(self.feed()["items"][0]["status"], "completed")

    def test_english_raises_headline_with_final_outcome_is_completed(self):
        self.add(
            "Teneo AI raises SEK 74 million in the rights issue",
            body="The Company has received the final outcome of the rights issue of SEK 74 million.",
            language="en",
        )
        self.assertEqual(self.feed()["items"][0]["status"], "completed")

    def test_page_chrome_does_not_assign_refinancing_loan_to_rights_issue(self):
        self.add(
            "Teneo AI intends to carry out a rights issue",
            body=("Teneo AI intends to carry out a rights issue. "
                  "The senior loan financing is approximately SEK 290 million. "
                  "The planned rights issue is at least SEK 75 million."),
            language="en",
        )
        item = self.feed()["items"][0]
        self.assertEqual(item["amount"], 75_000_000)

    def test_expected_proceeds_do_not_imply_final_outcome(self):
        self.add("Bolaget tillförs upp till 74 MSEK genom företrädesemission",
                 body="Det slutgiltiga utfallet kommer att offentliggöras efter teckningsperioden.")
        self.assertEqual(self.feed()["items"][0]["status"], "announced")

    def test_credit_facility_translation_with_incorrect_language_metadata(self):
        a, _ = self.add("WeSports Group ingår nytt kreditfacilitetsavtal om upp till 1 miljard kronor", language="en")
        b, _ = self.add("WeSports Group Enters into New Credit Facility Agreement of up to SEK 1 Billion", language="en")
        items = self.feed()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]["source_document_ids"]), {a, b})

    def test_translated_outcome_groups_despite_ambiguous_older_rounds(self):
        self.add("Företrädesemission om 20 MSEK", date="2026-08-01T08:00:00Z", body="Företrädesemission om 20 MSEK.")
        self.add("Företrädesemission om 30 MSEK", date="2026-08-10T08:00:00Z", body="Företrädesemission om 30 MSEK.")
        a, _ = self.add("Utfall i företrädesemission", body="Företrädesemissionens utfall publiceras.")
        b, _ = self.add("Outcome of rights issue", language="en", body="Outcome of rights issue.")
        items = self.feed()["items"]
        self.assertEqual(len(items), 3)
        outcomes = [item for item in items if a in item["source_document_ids"]]
        self.assertEqual(len(outcomes), 1)
        self.assertIn(b, outcomes[0]["source_document_ids"])

    def setUp(self):
        root = Path(".tmp-tests")
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "feed.db")
        self.db.initialize()
        self.next_id = 0

    def add(self, title, *, body="", date="2026-09-01T08:00:00Z", issuer="Testbolaget AB", language="sv"):
        self.next_id += 1
        external = f"fixture-{self.next_id}"
        url = f"https://mfn.se/a/testbolaget/{external}"
        self.db.upsert_document(DocumentCandidate(
            source_id="mfn", external_id=external, issuer_name=issuer,
            title=title, canonical_url=url, published_at=date, language=language,
        ), universe_only=False)
        with self.db.connect() as connection:
            document = connection.execute("SELECT document_id FROM document_sources WHERE source_external_id=?", (external,)).fetchone()[0]
            if body:
                connection.execute("""INSERT INTO document_contents
                    (document_id,source_url,extracted_text,text_sha256,char_count,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,'extracted',?,?)""",
                    (document, url, body, hashlib.sha256(body.encode()).hexdigest(), len(body), date, date))
        return document, url

    def feed(self, page=1):
        with self.db.connect() as connection:
            return get_transaction_feed(connection, page=page)

    def add_event(self, document_id, *, round_key, stage, **values):
        with self.db.connect() as connection:
            issuer_id = connection.execute(
                "SELECT issuer_id FROM documents WHERE id=?", (document_id,)
            ).fetchone()[0]
            content_id = connection.execute(
                "SELECT id FROM document_contents WHERE document_id=? ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO extraction_jobs
                   (issuer_id,document_id,content_id,provider,model,prompt_version,
                    input_sha256,selected_char_count,estimated_input_tokens,status,
                    attempt_count,started_at,completed_at)
                   VALUES (?,?,?,'test','test','test',?,1,1,'completed',1,?,?)""",
                (issuer_id, document_id, content_id, f"hash-{document_id}",
                 "2026-09-01T08:00:00Z", "2026-09-01T08:00:00Z"),
            )
            columns = {
                "issuer_id": issuer_id,
                "event_fingerprint": f"event-{document_id}",
                "event_type": "share_issue",
                "financing_type": "rights_issue",
                "description": "Företrädesemission",
                "announcement_date": "2026-09-01",
                "effective_date": "2026-09-01",
                "status": "completed" if stage == "outcome_published" else "announced",
                "cash_direction": "in",
                "currency": "SEK",
                "liquidity_effect_kind": "confirmed_cash" if stage == "outcome_published" else "conditional_liquidity",
                "reflected_in_report": "no",
                "confidence": 0.95,
                "created_at": "2026-09-01T08:00:00Z",
                "updated_at": "2026-09-01T08:00:00Z",
                "canonical_round_key": round_key,
                "lifecycle_stage": stage,
                "stage_effective_at": "2026-09-01",
                **values,
            }
            names = list(columns)
            placeholders = ",".join("?" for _ in names)
            event_id = connection.execute(
                f"INSERT INTO financing_events ({','.join(names)}) VALUES ({placeholders})",
                tuple(columns[name] for name in names),
            ).lastrowid
            connection.execute(
                """INSERT INTO financing_event_evidence
                   (financing_event_id,document_id,extraction_job_id,evidence_label,
                    source_excerpt,created_at) VALUES (?,?,?,'explicit',?,?)""",
                (event_id, document_id, cursor.lastrowid,
                 "Företrädesemission och garanti", "2026-09-01T08:00:00Z"),
            )
            return int(event_id)

    def test_announcement_is_replaced_by_linked_outcome_with_history(self):
        first, _ = self.add("Testbolaget beslutar om företrädesemission om 25 MSEK",
                            body="Företrädesemissionen beslutades den 1 september 2026. Bergs Securities är finansiell rådgivare.")
        old_id = self.feed()["items"][0]["id"]
        second, _ = self.add("Testbolaget offentliggör utfall i företrädesemission",
                             date="2026-09-06T08:00:00Z",
                             body="Företrädesemissionen beslutades den 1 september 2026. Bolaget tillförs 20 MSEK före emissionskostnader.")
        result = self.feed()
        self.assertEqual(result["total"], 1)
        latest = result["items"][0]
        self.assertEqual(latest["id"], old_id)
        self.assertEqual(latest["status_label"], "Utfall")
        self.assertEqual(latest["amount"], 20_000_000)
        self.assertEqual(latest["amount_basis"], "outcome_gross")
        self.assertEqual(latest["source_document_ids"], [first, second])
        self.assertEqual(latest["institutions"], [{"name": "Bergs Securities", "role": "Finansiell rådgivare"}])
        self.assertEqual(latest["details"]["announced_amount"], 25_000_000)
        self.assertEqual(latest["details"]["outcome_amount"], 20_000_000)
        self.assertEqual(latest["details"]["completion_ratio_pct"], 80.0)
        self.assertEqual([stage["status"] for stage in latest["details"]["lifecycle"]], ["approved", "completed"])

    def test_outcome_without_explicit_reference_links_to_single_recent_round(self):
        self.add("Testbolaget beslutar om företrädesemission om 25 MSEK")
        self.add(
            "Testbolaget offentliggör utfall i företrädesemission om 20 MSEK",
            date="2026-09-06T08:00:00Z",
        )
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)
        self.assertEqual(result["items"][0]["status"], "completed")

    def test_explicit_source_link_survives_changed_timetable_dates(self):
        first, url = self.add(
            "Testbolaget beslutar om företrädesemission om 25 MSEK",
            body="Företrädesemissionen beslutades den 1 september 2026.",
        )
        second, _ = self.add(
            "Testbolaget offentliggör utfall i företrädesemission",
            date="2026-09-20T08:00:00Z",
            body=("Teckningsperioden i företrädesemissionen avslutades den 18 september 2026. "
                  f"Emissionsvillkor: {url}"),
        )
        items = self.feed()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]["source_document_ids"]), {first, second})

    def test_upsized_directed_bookbuild_links_explicit_same_day_completion(self):
        first, _ = self.add(
            "Climeon utreder förutsättningarna för att genomföra en riktad nyemission om 40 MSEK",
            body="Ett accelererat bookbuilding-förfarande inleds omedelbart.",
            date="2026-09-09T17:40:00+02:00",
        )
        second, _ = self.add(
            "Climeon genomför en riktad nyemission om 50 MSEK",
            body=("Bookbuilding-förfarandet som offentliggjordes av Bolaget tidigare idag "
                  "har slutförts och Climeon genomför en riktad nyemission om 50 MSEK."),
            date="2026-09-09T23:49:00+02:00",
        )
        items = self.feed()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0]["source_document_ids"]), {first, second})
        launch = next(source for source in items[0]["history"] if source["document_id"] == first)
        self.assertEqual(launch["status"], "proposed")

    def test_directed_bookbuild_without_explicit_completion_link_stays_separate(self):
        self.add(
            "Testbolaget utreder förutsättningarna för en riktad emission om 40 MSEK",
            body="Ett accelererat bookbuilding-förfarande inleds omedelbart.",
            date="2026-09-09T17:40:00+02:00",
        )
        self.add(
            "Testbolaget genomför en riktad emission om 50 MSEK",
            body="En riktad emission om 50 MSEK genomförs.",
            date="2026-09-09T23:49:00+02:00",
        )
        self.assertEqual(self.feed()["total"], 2)

    def test_explicit_source_link_does_not_override_distinct_round_keys(self):
        first, url = self.add(
            "Testbolaget beslutar om företrädesemission om 25 MSEK",
            body="Företrädesemissionen beslutades den 1 september 2026.",
        )
        second, _ = self.add(
            "Testbolaget offentliggör utfall i företrädesemission",
            date="2026-09-20T08:00:00Z",
            body=f"Företrädesemissionens utfall. Se även tidigare emission {url}",
        )
        self.add_event(first, round_key="test-round-a", stage="announced")
        self.add_event(second, round_key="test-round-b", stage="outcome_published")
        self.assertEqual(self.feed()["total"], 2)

    def test_legacy_round_without_key_merges_successive_compatible_stages(self):
        self.add(
            "Testbolaget föreslår fullt garanterad företrädesemission om cirka 36 MSEK",
            date="2026-07-27T08:30:00Z",
        )
        self.add(
            "Testbolaget offentliggör informationsbroschyr avseende företrädesemission om cirka 36 MSEK",
            date="2026-08-20T11:00:00Z",
        )
        self.add(
            "Testbolaget offentliggör utfall i företrädesemissionen om 36 MSEK",
            date="2026-09-08T17:30:00Z",
        )
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["status"], "completed")
        self.assertEqual(len(result["items"][0]["source_document_ids"]), 3)

    def test_teneo_lifecycle_is_one_rights_round_and_non_cash_directed_issue_stays_separate(self):
        proposed, _ = self.add(
            "Teneo AI föreslår företrädesemission om cirka 75 MSEK",
            body="Styrelsen föreslår en företrädesemission om cirka 75 MSEK.",
            date="2026-07-27T07:30:00+02:00",
            issuer="Teneo AI AB",
        )
        terms, _ = self.add(
            "Teneo AI beslutar om villkor för företrädesemissionen",
            body="Styrelsen har beslutat om villkoren för företrädesemissionen.",
            date="2026-08-10T07:30:00+02:00",
            issuer="Teneo AI AB",
        )
        preliminary, _ = self.add(
            "Teneo AI offentliggör preliminärt utfall i företrädesemissionen om 74 MSEK",
            body="Det preliminära utfallet i företrädesemissionen uppgår till 74 MSEK.",
            date="2026-09-02T22:40:00+02:00",
            issuer="Teneo AI AB",
        )
        final, _ = self.add(
            "Teneo AI tillförs 74 MSEK genom företrädesemissionen",
            body="Teneo AI har erhållit det slutgiltiga utfallet i företrädesemissionen om 74 MSEK.",
            date="2026-09-03T17:55:00+02:00",
            issuer="Teneo AI AB",
        )
        directed, _ = self.add(
            "Teneo AI har slutfört riktade emissioner och refinansieringen",
            body=("Betalning i de riktade emissionerna skedde genom kvittning av fordringar "
                  "och bolagets räntebärande skuld är nu helt eliminerad. Ingen kontant "
                  "likvid tillfördes bolaget."),
            date="2026-09-16T19:30:00+02:00",
            issuer="Teneo AI AB",
        )

        result = self.feed()
        self.assertEqual(result["total"], 2)
        rights = next(item for item in result["items"] if item["category"] == "rights_issue")
        placement = next(item for item in result["items"] if item["category"] == "directed_issue")
        self.assertEqual(
            set(rights["source_document_ids"]),
            {proposed, terms, preliminary, final},
        )
        self.assertEqual(rights["status"], "completed")
        self.assertEqual(rights["amount"], 74_000_000)
        self.assertEqual(rights["details"]["announced_amount"], 75_000_000)
        self.assertEqual(rights["details"]["outcome_amount"], 74_000_000)
        self.assertAlmostEqual(rights["details"]["completion_ratio_pct"], 98.7)
        self.assertEqual(placement["source_document_ids"], [directed])
        self.assertIsNone(placement["amount"])
        self.assertIsNone(placement["details"]["outcome_amount"])
        self.assertFalse(
            placement["details"]["outcome_amount_basis"] == "directed_issue_cash_assumption"
        )

    def test_ambiguous_outcome_does_not_merge_two_recent_rounds(self):
        self.add("Testbolaget beslutar om företrädesemission om 10 MSEK")
        self.add("Testbolaget beslutar om företrädesemission om 20 MSEK", date="2026-09-02T08:00:00Z")
        self.add("Testbolaget offentliggör utfall i företrädesemission", date="2026-09-06T08:00:00Z")
        result = self.feed()
        self.assertEqual(result["total"], 3)
        self.assertTrue(any(item["status"] == "completed" and item["review_required"] for item in result["items"]))

    def test_round_key_resolves_ambiguous_lifecycle_and_builds_net_dossier(self):
        opening, _ = self.add(
            "Testbolaget beslutar om företrädesemission om 10 MSEK",
            body=("Emissionen är säkerställd genom teckningsförbindelser och garantier. "
                  "Garantiersättning uppgår till 12 procent."),
        )
        other, _ = self.add(
            "Testbolaget beslutar om företrädesemission om 20 MSEK",
            date="2026-09-02T08:00:00Z",
            body="En separat företrädesemission om 20 MSEK.",
        )
        outcome, _ = self.add(
            "Testbolaget offentliggör utfall i företrädesemission om 10 MSEK",
            date="2026-09-06T08:00:00Z",
            body=("Företrädesemissionen tillför bolaget cirka 10 MSEK före kostnader. "
                  "Emissionskostnader uppgår till 1 MSEK exklusive garantiersättning. "
                  "Garantiersättningen beräknas uppgå till 0,6 MSEK."),
        )
        self.add_event(
            opening, round_key="test-rights-2026-a", stage="announced",
            gross_amount=10_000_000, subscription_commitment_amount=2_000_000,
            subscription_commitment_pct=0.2, guarantee_commitment_amount=8_000_000,
            guarantee_commitment_pct=0.8, secured_total_amount=10_000_000,
            secured_total_pct=1.0, guarantee_fee_details="12 procent kontant ersättning",
            issue_price=0.25,
        )
        self.add_event(
            other, round_key="test-rights-2026-b", stage="announced",
            gross_amount=20_000_000,
        )
        self.add_event(
            outcome, round_key="test-rights-2026-a", stage="outcome_published",
            gross_amount=10_000_000, final_subscription_amount=10_000_000,
            final_subscription_pct=1.0, guarantee_utilized_amount=6_000_000,
            guarantee_utilized_pct=0.75, subscription_without_guarantees_pct=0.4,
            transaction_cost=1_000_000, outcome="Fulltecknad efter garantiutnyttjande",
        )

        result = self.feed()
        self.assertEqual(result["total"], 2)
        item = next(value for value in result["items"] if value["status"] == "completed")
        self.assertEqual(item["source_document_ids"], [opening, outcome])
        dossier = item["details"]["financing_dossier"]
        self.assertEqual(dossier["canonical_round_key"], "test-rights-2026-a")
        self.assertEqual(dossier["guarantee_fee_amount"], 600_000)
        self.assertAlmostEqual(dossier["guarantee_fee_pct"], 0.12)
        self.assertEqual(dossier["transaction_cost_total"], 1_600_000)
        self.assertEqual(dossier["net_amount"], 8_400_000)
        self.assertEqual(dossier["net_amount_basis"], "derived_gross_less_source_costs")
        self.assertEqual(dossier["guarantee_utilized_amount"], 6_000_000)
        reconciliation = dossier["round_reconciliation"]
        self.assertEqual(reconciliation["version"], "source_bound_round_v1")
        self.assertEqual(
            reconciliation["terms"]["guarantee_commitment_amount"]["value"], 8_000_000,
        )
        self.assertEqual(
            reconciliation["actual_outcome"]["guarantee_utilized_amount"]["value"], 6_000_000,
        )
        self.assertEqual(
            reconciliation["costs_and_net_proceeds"]["net_amount"]["value"], 8_400_000,
        )
        self.assertEqual(
            reconciliation["costs_and_net_proceeds"]["net_amount"]["basis"],
            "derived_from_source_values",
        )
        self.assertEqual(reconciliation["derived"]["announced_to_actual_gross_pct"], 100.0)
        self.assertEqual(reconciliation["derived"]["unused_guarantee_commitment_amount"], 2_000_000)
        self.assertEqual(
            reconciliation["actual_outcome"]["guarantee_utilized_amount"]["source_document_ids"],
            [outcome],
        )

    def test_legacy_outcome_builds_source_bound_subscription_and_cost_dossier(self):
        self.add(
            "Testbolaget föreslår fullt garanterad företrädesemission om cirka 36 MSEK",
            body="Teckningskursen i företrädesemissionen är SEK 0,20 per unit.",
            date="2026-07-27T08:30:00Z",
            language="sv",
        )
        self.add(
            "Testbolaget announces outcome of the company’s rights issue",
            body=(
                "The Rights Issue has been subscribed for in full, of which approximately "
                "34.4 percent was subscribed for by exercise of unit rights, approximately "
                "6.5 percent was subscribed for without exercise of unit rights and "
                "approximately 59.1 percent was subscribed for by the underwriters. "
                "Through the Rights Issue, the Company will receive gross proceeds of "
                "approximately SEK 36 million before issue costs. The Company estimates "
                "that issue costs will amount to approximately SEK 4 million excluding "
                "underwriting fees, which are estimated at approximately SEK 3 million. "
                "The underwriting fee amounts to 12 percent of the underwritten amount. "
                "DNB Carnegie Investment Bank AB and Mangold Fondkommission AB have entered "
                "into underwriting commitments in connection with the Rights Issue. "
                "Testbolaget has engaged DNB Carnegie, DNB Carnegie Investment Bank AB "
                "(publ) and Redeye AB as financial advisers in connection with the Rights Issue."
            ),
            date="2026-09-08T17:30:00Z",
            language="en",
        )

        item = self.feed()["items"][0]
        dossier = item["details"]["financing_dossier"]
        self.assertEqual(dossier["gross_amount"], 36_000_000)
        self.assertEqual(dossier["transaction_cost"], 4_000_000)
        self.assertEqual(dossier["guarantee_fee_amount"], 3_000_000)
        self.assertAlmostEqual(dossier["guarantee_fee_pct"], 0.12)
        self.assertEqual(dossier["transaction_cost_total"], 7_000_000)
        self.assertEqual(dossier["net_amount"], 29_000_000)
        self.assertEqual(dossier["net_amount_basis"], "derived_gross_less_source_costs")
        self.assertAlmostEqual(dossier["final_subscription_pct"], 1.0)
        self.assertAlmostEqual(dossier["subscription_with_rights_pct"], 0.344)
        self.assertAlmostEqual(dossier["subscription_without_rights_pct"], 0.065)
        self.assertAlmostEqual(dossier["subscription_without_guarantees_pct"], 0.409)
        self.assertAlmostEqual(dossier["guarantee_take_up_pct_of_issue"], 0.591)
        self.assertEqual(dossier["issue_price"], 0.2)
        self.assertIn(
            {"name": "DNB Carnegie", "role": "Finansiell rådgivare"},
            item["institutions"],
        )
        self.assertIn(
            {"name": "Redeye AB", "role": "Finansiell rådgivare"},
            item["institutions"],
        )
        self.assertIn(
            {"name": "DNB Carnegie Investment Bank AB", "role": "Garant"},
            item["institutions"],
        )

    def test_guarantee_compensation_shares_do_not_become_share_capital_fee(self):
        self.add(
            "Teneo AI offentliggör utfall i företrädesemission om 74 MSEK",
            body=("För det fall att garanten i Företrädesemissionen väljer att erhålla hela "
                  "garantiersättningen i form av aktier kommer det högsta antalet aktier "
                  "att öka med ytterligare högst 142 920 000, från 1 972 576 960 till "
                  "högst 2 115 496 960 och aktiekapitalet kommer att öka med högst "
                  "7 045 241,40 SEK, från 153 237 217,46 SEK till högst 160 282 458,86 SEK."),
        )
        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        self.assertIsNone(dossier["guarantee_fee_amount"])
        self.assertIsNone(dossier["guarantee_fee_pct"])

    def test_fee_rate_does_not_reuse_subscription_percentage_before_fee_marker(self):
        self.add(
            "Utfall i företrädesemission om 20 MSEK",
            body="Emissionen tecknades till 90 procent och garantiersättningen är 2 MSEK.",
        )
        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        self.assertEqual(dossier["guarantee_fee_amount"], 2_000_000)
        self.assertIsNone(dossier["guarantee_fee_pct"])

    def test_dossier_projects_source_bound_top_middle_and_bottom_guarantees(self):
        document, _ = self.add(
            "Testbolaget beslutar om företrädesemission om 10 MSEK",
            body="Emissionen omfattar olika garantinivåer.",
        )
        event_id = self.add_event(
            document,
            round_key="test-rights-2026", stage="terms_published",
            guarantee_commitment_amount=10_000_000,
        )
        with self.db.connect() as connection:
            now = "2026-09-01T08:00:00Z"
            for tier, commitment, fee_rate, fee_amount, used in (
                ("top", 3_000_000, 0.14, None, None),
                ("middle", 4_000_000, 0.12, None, 500_000),
                ("bottom", 3_000_000, 0.10, 300_000, None),
            ):
                connection.execute(
                    """INSERT INTO financing_event_guarantee_tranches (
                        financing_event_id, tier, commitment_amount, fee_rate,
                        fee_amount, utilized_amount, source_document_id,
                        source_excerpt, confidence, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, tier, commitment, fee_rate, fee_amount, used,
                     document, f"{tier} guarantee", 0.95, now, now),
                )

        dossier = self.feed()["items"][0]["details"]["financing_dossier"]
        tranches = dossier["guarantee_tranches"]
        self.assertEqual([tranche["tier"] for tranche in tranches], ["top", "middle", "bottom"])
        self.assertEqual(tranches[0]["commitment_amount"], {
            "value": 3_000_000,
            "source_document_ids": [document],
            "basis": "structured_source",
        })
        self.assertEqual(tranches[1]["utilized_amount"]["value"], 500_000)
        self.assertEqual(tranches[2]["fee_amount"]["value"], 300_000)
        self.assertEqual(
            dossier["round_reconciliation"]["guarantee_tranches"], tranches,
        )

    def test_distinct_rounds_and_bridge_loan_do_not_merge(self):
        self.add("Testbolaget beslutar om företrädesemission om 25 MSEK",
                 body="Företrädesemissionen beslutades den 1 september 2026.")
        self.add("Testbolaget beslutar om företrädesemission om 30 MSEK",
                 body="Företrädesemissionen beslutades den 5 september 2026.", date="2026-09-05T08:00:00Z")
        self.add("Testbolaget upptar brygglån om 5 MSEK", date="2026-09-05T09:00:00Z")
        result = self.feed()
        self.assertEqual(result["total"], 3)
        self.assertEqual({i["amount"] for i in result["items"]}, {25_000_000, 30_000_000, 5_000_000})

    def test_bridge_loan_without_same_source_equity_announcement_is_red(self):
        self.add("Testbolaget upptar brygglån om 5 MSEK")

        bridge = self.feed()["items"][0]

        self.assertEqual(bridge["category"], "bridge_loan")
        self.assertEqual(bridge["signal_tone"], "red")
        self.assertEqual(
            bridge["signal_label"], "Brygglån utan samtidigt emissionsbesked",
        )
        self.assertEqual(bridge["details"]["bridge_loan_alert"], {
            "severity": "high",
            "equity_plan_announced_with_bridge": False,
            "linked_equity_source_document_ids": [],
            "message": (
                "Brygglån utan samtidigt offentligt emissionsbesked i den "
                "källhistorik som hör till affären."
            ),
        })

    def test_bridge_loan_same_source_directed_issue_clears_warning(self):
        document, _ = self.add(
            "Testbolaget upptar brygglån och beslutar om riktad emission om 5 MSEK"
        )

        items = self.feed()["items"]
        bridge = next(item for item in items if item["category"] == "bridge_loan")

        self.assertEqual(bridge["signal_tone"], "neutral")
        self.assertEqual(bridge["details"]["bridge_loan_alert"], {
            "severity": "info",
            "equity_plan_announced_with_bridge": True,
            "linked_equity_source_document_ids": [document],
            "message": "Brygglånet offentliggjordes tillsammans med ett emissionsbesked.",
        })

    def test_report_order_and_incentive_news_are_excluded(self):
        for title in ("Delårsrapport januari–juni med genomförd företrädesemission",
                      "Bolaget erhåller order om 20 MSEK",
                      "Kallelse till årsstämma: riktad emission",
                      "Teckningsoptioner inom incitamentsprogram"):
            self.add(title)
        self.assertEqual(self.feed()["total"], 0)

    def test_two_placements_decided_same_day_with_different_sizes_stay_separate(self):
        for amount in (10, 20):
            self.add(f"Testbolaget beslutar om riktad emission om {amount} MSEK",
                     body="Den riktade emissionen beslutades den 1 september 2026.",
                     date=f"2026-09-01T08:{amount:02}:00Z")
        self.assertEqual(self.feed()["total"], 2)

    def test_translation_pairs_preserve_sources_without_double_count(self):
        self.add("Testbolaget AB beslutar om företrädesemission om 25 MSEK", language="sv")
        self.add("Testbolaget AB resolves on a rights issue of SEK 25 million",
                 date="2026-09-01T08:01:00Z", language="en")
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)

    def test_same_day_same_stage_updates_enrich_one_transaction(self):
        self.add(
            "Testbolaget offentliggör utfall i företrädesemissionen",
            body="Företrädesemissionens utfall har fastställts. Rådgivarbolaget är finansiell rådgivare.",
        )
        self.add(
            "Testbolaget offentliggör slutligt utfall i företrädesemissionen om 20 MSEK",
            body="Företrädesemissionen tillför bolaget 20 MSEK före emissionskostnader.",
            date="2026-09-01T08:15:00Z",
        )
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)
        self.assertEqual(result["items"][0]["amount"], 20_000_000)

    def test_delayed_same_day_translation_with_amount_in_one_version_is_one_transaction(self):
        self.add(
            "Testbolaget offentliggör utfall i företrädesemissionen",
            language="sv",
        )
        self.add(
            "Testbolaget announces outcome of the rights issue of SEK 20 million",
            date="2026-09-01T09:30:00Z",
            language="en",
        )
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)
        self.assertEqual(result["items"][0]["amount"], 20_000_000)

    def test_title_language_overrides_incorrect_mfn_language_metadata(self):
        self.add("Testbolaget offentliggör utfall i företrädesemissionen", language="sv")
        self.add(
            "Testbolaget announces outcome of the rights issue of SEK 20 million",
            date="2026-09-01T09:30:00Z",
            language="sv",
        )
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)

    def test_same_day_same_stage_with_conflicting_amounts_stays_separate(self):
        self.add("Testbolaget offentliggör utfall i företrädesemissionen om 10 MSEK")
        self.add(
            "Testbolaget offentliggör utfall i företrädesemissionen om 20 MSEK",
            date="2026-09-01T08:15:00Z",
        )
        self.assertEqual(self.feed()["total"], 2)

    def test_bilingual_pair_with_different_wording_is_one_transaction(self):
        _, swedish_url = self.add("Testbolaget beslutar om företrädesemission om 25 MSEK", language="sv")
        self.add("Testbolaget announces a rights issue of SEK 25 million",
                 date="2026-09-01T08:01:00Z", language="en")
        result = self.feed()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"][0]["history"]), 2)
        self.assertEqual(result["items"][0]["source_url"], swedish_url)

    def test_emission_signals_follow_subscription_outcome_and_type(self):
        self.add("Testbolaget offentliggör utfall av fulltecknad företrädesemission om 25 MSEK")
        item = self.feed()["items"][0]
        self.assertEqual((item["signal_tone"], item["signal_label"]), ("green", "Fulltecknad emission"))

        self.add("Annat Bolag beslutar om riktad emission om 10 MSEK", issuer="Annat Bolag AB")
        self.add("Tredje Bolag offentliggör utfall av undertecknad företrädesemission", issuer="Tredje Bolag AB")
        signals = {(item["issuer_name"], item["signal_tone"]) for item in self.feed()["items"]}
        self.assertIn(("Annat Bolag AB", "green"), signals)
        self.assertIn(("Tredje Bolag AB", "red"), signals)

    def test_signal_waits_for_outcome_and_does_not_read_old_terms_as_result(self):
        self.add(
            "Testbolaget beslutar om företrädesemission om högst 25 MSEK vid full teckning"
        )
        item = self.feed()["items"][0]
        self.assertEqual((item["signal_tone"], item["signal_label"]), ("yellow", "Planerad emission"))

    def test_cancelled_emission_is_red_and_directed_issue_is_green(self):
        self.add("Testbolaget ställer in företrädesemission")
        self.add("Annat Bolag beslutar om riktad emission om 10 MSEK", issuer="Annat Bolag AB")
        signals = {(item["issuer_name"], item["signal_tone"], item["signal_label"])
                   for item in self.feed()["items"]}
        self.assertIn(("Testbolaget AB", "red", "Inställd emission"), signals)
        self.assertIn(("Annat Bolag AB", "green", "Riktad emission"), signals)

    def test_directed_issue_fills_outcome_immediately(self):
        self.add("Testbolaget beslutar om riktad emission om 10 MSEK")
        item = self.feed()["items"][0]
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["status_label"], "Beslutad")
        self.assertEqual(item["details"]["announced_amount"], 10_000_000)
        self.assertEqual(item["details"]["outcome_amount"], 10_000_000)
        self.assertEqual(item["details"]["outcome_amount_basis"], "directed_issue_cash_assumption")
        self.assertEqual(item["details"]["completion_ratio_pct"], 100.0)

    def test_directed_issue_cash_outcome_requires_decided_cash_amount(self):
        cases = (
            "Testbolaget föreslår riktad emission om högst 10 MSEK",
            "Testbolaget offentliggör villkorad riktad emission om 10 MSEK",
            "Testbolaget beslutar om riktad emission om 10 MSEK genom kvittning utan kontant likvid",
        )
        for index, title in enumerate(cases):
            with self.subTest(title=title):
                issuer = f"Unsafe Directed {index} AB"
                self.add(title, issuer=issuer)
                item = next(item for item in self.feed()["items"] if item["issuer_name"] == issuer)
                self.assertIsNone(item["details"]["outcome_amount"])
                self.assertNotEqual(item["signal_tone"], "green")

    def test_directed_issue_assumption_survives_reminder_and_preserves_registration(self):
        _, announcement_url = self.add("Testbolaget beslutar om riktad emission om 10 MSEK")
        self.add("Testbolaget uppdaterar villkoren för den riktade emissionen",
                 body=f"Tidigare besked: {announcement_url}", date="2026-09-02T08:00:00Z")
        item = self.feed()["items"][0]
        self.assertEqual(item["details"]["outcome_amount"], 10_000_000)
        self.assertEqual(item["details"]["outcome_amount_basis"], "directed_issue_cash_assumption")

        self.add("Testbolaget registrerar den riktade emissionen om 10 MSEK",
                 date="2026-09-03T08:00:00Z")
        item = self.feed()["items"][0]
        self.assertEqual(item["status"], "registered")
        self.assertEqual(item["details"]["outcome_amount_basis"], "outcome_amount")

    def test_unknown_subscription_outcome_is_neutral(self):
        self.add("Testbolaget offentliggör utfall i företrädesemission")
        item = self.feed()["items"][0]
        self.assertEqual(
            (item["signal_tone"], item["signal_label"]),
            ("neutral", "Utfall eller villkor utan teckningsgrad"),
        )

    def test_warrant_series_and_unrelated_outcomes_stay_separate(self):
        self.add("Testbolaget: teckningsoptioner TO1", body="Teckningsoptioner av serie TO1.")
        self.add("Testbolaget: utfall av teckningsoptioner TO1", body="Teckningsoptioner av serie TO1.", date="2026-09-05T08:00:00Z")
        self.add("Testbolaget: utfall av teckningsoptioner TO2", body="Teckningsoptioner av serie TO2.", date="2026-09-06T08:00:00Z")
        self.assertEqual(self.feed()["total"], 2)

    def test_unknown_outcome_does_not_reuse_announced_maximum(self):
        self.add("Testbolaget beslutar om företrädesemission om högst 25 MSEK",
                 body="Företrädesemissionen beslutades den 1 september 2026.")
        self.add("Testbolaget meddelar utfall av företrädesemission",
                 body="Företrädesemissionen beslutades den 1 september 2026. Vid full teckning kan företrädesemissionen tillföra högst 25 MSEK.",
                 date="2026-09-06T08:00:00Z")
        item = self.feed()["items"][0]
        self.assertEqual(self.feed()["total"], 1)
        self.assertIsNone(item["amount"])
        self.assertTrue(item["review_required"])

    def test_late_prospectus_update_does_not_regress_published_outcome(self):
        self.add(
            "Testbolaget offentliggör utfall i företrädesemission",
            body=(
                "Företrädesemissionen beslutades den 1 september 2026. "
                "Bolaget tillförs 20 MSEK före emissionskostnader."
            ),
        )
        self.add(
            "Testbolaget uppdaterar prospekt för företrädesemission",
            body="Företrädesemissionen beslutades den 1 september 2026.",
            date="2026-09-07T08:00:00Z",
        )
        item = self.feed()["items"][0]
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["status_label"], "Utfall")
        self.assertEqual(item["amount"], 20_000_000)

    def test_negated_registration_does_not_advance_lifecycle(self):
        self.add(
            "Företrädesemissionen har ännu inte registrerats",
            body="Registrering återstår efter företrädesemissionen.",
        )
        item = self.feed()["items"][0]
        self.assertEqual(item["status"], "updated")
        self.assertNotEqual(item["status_label"], "Registrerad")

    def test_late_administrative_update_does_not_hide_cancellation(self):
        self.add(
            "Testbolaget ställer in företrädesemission",
            body="Företrädesemissionen beslutades den 1 september 2026.",
        )
        self.add(
            "Testbolaget uppdaterar information om företrädesemission",
            body="Företrädesemissionen beslutades den 1 september 2026.",
            date="2026-09-07T08:00:00Z",
        )
        item = self.feed()["items"][0]
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(item["status_label"], "Inställd")

    def test_institution_role_is_not_inferred_from_certified_adviser_footer(self):
        self.add("Testbolaget upptar brygglån om 5 MSEK",
                 body="Testbanken är långivare. Annan Bank är Certified Adviser.")
        self.assertEqual(self.feed()["items"][0]["institutions"], [{"name": "Testbanken", "role": "Långivare"}])

    def test_grouping_happens_before_pagination_and_read_is_nonmutating(self):
        for index in range(12):
            self.add("Riktad nyemission om 10 MSEK", issuer=f"Bolag {index} AB")
        with self.db.connect() as connection:
            changes = connection.total_changes
            first = get_transaction_feed(connection)
            second = get_transaction_feed(connection, page=2)
            self.assertEqual(connection.total_changes, changes)
        self.assertEqual((len(first["items"]), len(second["items"])), (10, 2))
        self.assertEqual(first["total"], 12)
        self.assertFalse({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})


if __name__ == "__main__":
    unittest.main()
