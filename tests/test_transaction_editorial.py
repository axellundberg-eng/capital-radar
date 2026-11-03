from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.ai.response import ProviderResponse, ProviderUsage
from capital_radar.agent_runner import reset_pools
from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.extraction_v2 import _persist_financing_events
from capital_radar.models import SecurityCandidate
from capital_radar.normalization import utc_now
from capital_radar.transaction_editorial import (
    TransactionEditorialService,
    _evidence_number_aliases,
    _select_evidence_sections,
    _scaled_fact_numbers,
)


VALID_SUMMARY = (
    "Editorial AB har offentliggjort en företrädesemission om 10 MSEK. "
    "Det annonserade beloppet avser kapitalanskaffningen enligt bolagets "
    "pressmeddelande. Närmare uppgifter om teckningsvillkor och deltagande "
    "finansiella institutioner framgår inte av det tillgängliga underlaget "
    "för denna sammanställning."
)
VALID_DETAIL = (
    "Affären följs som ett sammanhängande kapitalmarknadsförlopp där det senaste "
    "offentliggjorda beskedet bestämmer aktuell status. Underlaget beskriver "
    "transaktionstypen och det kommunicerade kapitalbeloppet, medan ej uttryckligen "
    "angivna villkor lämnas öppna. Kommande bolagsmeddelanden kan därför komplettera "
    "akten med teckningsgrad, slutlig likvid, kapitalanvändning, rådgivare och nästa "
    "formella milstolpe utan att den ursprungliga affären dubbelräknas."
)
VALID_DETAIL = " ".join([VALID_DETAIL] * 5)


def editorial_payload(headline: str, summary: str = VALID_SUMMARY, **overrides):
    payload = {
        "headline": headline,
        "summary": summary,
        "detail_summary": VALID_DETAIL,
        "key_points": [
            "Transaktionen följs från offentliggörande till slutligt utfall.",
            "Endast uttryckligen källbelagda villkor presenteras.",
        ],
        "terms": [],
        "use_of_proceeds": None,
        "next_milestone": None,
    }
    payload.update(overrides)
    return payload


class _Provider:
    provider_name = "fake-pro"

    def __init__(self, payloads: list[dict[str, str]]) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.last_kwargs = None

    def structured_completion(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return ProviderResponse(
            provider="deepseek",
            model="fake-pro",
            content=json.dumps(self.payloads.pop(0), ensure_ascii=False),
            tool_calls=[],
            finish_reason="stop",
            usage=ProviderUsage(input_tokens=10, cached_input_tokens=0, output_tokens=10),
            latency_ms=1,
            request_id=f"req-{self.calls}",
        )


def _settings(**overrides):
    values = {
        "deepseek_flash_model": "deepseek-v4-flash",
        "deepseek_flash_max_output_tokens": 4096,
        "deepseek_flash_connect_timeout_seconds": 10.0,
        "deepseek_flash_read_timeout_seconds": 45.0,
        "deepseek_flash_hard_timeout_seconds": 60.0,
        "deepseek_pro_model": "deepseek-v4-pro",
        "deepseek_pro_max_output_tokens": 8192,
        "deepseek_pro_connect_timeout_seconds": 30.0,
        "deepseek_pro_read_timeout_seconds": 300.0,
        "deepseek_pro_hard_timeout_seconds": 360.0,
        "deepseek_pro_reasoning": "high",
        "extraction_max_input_chars": 12_000,
        "private_data_external_egress_enabled": False,
        "deepseek_api_key": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TransactionEditorialTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pools()
        self.test_dir = Path("data") / f"test-transaction-editorial-{uuid4().hex}"
        self.test_dir.mkdir(parents=True, exist_ok=False)
        self.db = Database(self.test_dir / "editorial.db")
        self.db.initialize()
        run = self.db.start_run("nasdaq_nordic", "fixture", "fixture")
        self.db.upsert_security(
            SecurityCandidate(
                source_id="nasdaq_nordic", external_id="EDIT-1", issuer_name="Editorial AB",
                display_name="Editorial", symbol="EDIT", isin="SE0000001111",
                currency="SEK", market="Nasdaq Stockholm", segment="Small Cap",
            ),
            run,
        )
        self.db.finish_run(run, status="success")
        with self.db.connect() as connection:
            self.issuer_id = int(connection.execute("SELECT id FROM issuers").fetchone()[0])
        self.document_id = self._add_source("Bolaget offentliggjorde en företrädesemission om 10 MSEK.")

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _add_source(self, text: str) -> int:
        now = utc_now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.db.connect() as connection:
            document_id = int(connection.execute(
                """INSERT INTO documents (issuer_id,event_key,canonical_url,title,published_at,
                   profile_category,cash_relevance_score,relevance_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,'financing',100,'fixture',?,?)""",
                (self.issuer_id, f"fixture-{digest}", "https://example.test/source", "Emission", now, now, now),
            ).lastrowid)
            connection.execute(
                """INSERT INTO document_contents (document_id,source_url,extracted_text,text_sha256,
                   char_count,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,'extracted',?,?)""",
                (document_id, "https://example.test/source", text, digest, len(text), now, now),
            )
        return document_id

    def _item(self, document_id: int | None = None) -> dict:
        return {
            "id": "transaction:edit-1",
            "issuer_id": self.issuer_id,
            "issuer_name": "Editorial AB",
            "category": "rights_issue",
            "category_label": "Företrädesemission",
            "status": "announced",
            "status_label": "Offentliggjord",
            "headline": "Editorial AB offentliggör företrädesemission",
            "summary": "Bolaget har offentliggjort en företrädesemission enligt källan.",
            "amount": 10,
            "currency": "MSEK",
            "amount_basis": "gross",
            "institutions": [],
            "source_document_ids": [document_id or self.document_id],
            "editorial_source": "source",
        }

    def test_process_is_idempotent_and_cached_overlay_is_read_only(self) -> None:
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        self.assertTrue(service.process_item(item))
        self.assertTrue(service.process_item(item))
        self.assertEqual(provider.calls, 1)
        with self.db.connect() as connection:
            overlaid = service.apply_cached_editorial(connection, item)
        self.assertEqual(overlaid["editorial_source"], "ai")
        self.assertEqual(overlaid["headline"], "Editorial AB: offentliggjord emission")
        self.assertEqual(overlaid["details"]["detail_summary"], VALID_DETAIL)
        self.assertEqual(len(overlaid["details"]["key_points"]), 2)
        self.assertGreaterEqual(len(VALID_SUMMARY.split()), 30)
        self.assertLessEqual(len(VALID_SUMMARY.split()), 90)

    def test_new_source_hash_invalidates_cache(self) -> None:
        second_document = self._add_source("Bolaget offentliggjorde en företrädesemission om 20 MSEK.")
        provider = _Provider([
            editorial_payload("Editorial AB: offentliggjord emission"),
            editorial_payload("Editorial AB: offentliggjord företrädesemission", VALID_SUMMARY.replace("10 MSEK", "20 MSEK")),
        ])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        self.assertTrue(service.process_item(self._item()))
        latest = self._item(second_document)
        self.assertTrue(service.process_item(latest))
        self.assertEqual(provider.calls, 2)
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, latest)["headline"], "Editorial AB: offentliggjord företrädesemission")

    def test_pro_agent_receives_bound_multi_source_packet(self) -> None:
        second_document = self._add_source(
            "Bolaget kompletterade emissionen om 10 MSEK med uppgift om kommande registrering."
        )
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        item["source_document_ids"] = [self.document_id, second_document]

        self.assertEqual(service._profile.model, "deepseek-v4-pro")
        self.assertFalse(service._profile.thinking_enabled)
        self.assertEqual(service._profile.concurrency_pool, "analysis_pro")
        self.assertEqual(service._profile.hard_timeout, 60.0)
        self.assertTrue(service.process_item(item))

        request = provider.last_kwargs
        self.assertEqual(request["thinking"], "disabled")
        prompt = json.loads(request["user_prompt"])
        self.assertEqual(
            set(prompt["source_packet"]["document_ids"]),
            {self.document_id, second_document},
        )
        self.assertEqual(len(prompt["source_packet"]["manifest"]), 2)
        self.assertEqual(
            set(prompt["facts"]["source_document_ids"]),
            {self.document_id, second_document},
        )
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT content_json,evidence_json FROM agent_artifacts ORDER BY id DESC LIMIT 1"
            ).fetchone()
        content = json.loads(row["content_json"])
        evidence = json.loads(row["evidence_json"])
        expected_ids = ",".join(
            str(value) for value in prompt["source_packet"]["document_ids"]
        )
        self.assertEqual(content["_binding"]["source_document_ids"], expected_ids)
        self.assertEqual(evidence[0]["source_document_ids"], expected_ids)
        reduced_item = dict(item)
        reduced_item["source_document_ids"] = [second_document]
        with self.db.connect() as connection:
            self.assertEqual(
                service.apply_cached_editorial(connection, reduced_item)["editorial_source"],
                "source",
            )

    def test_editorial_packet_keeps_older_financing_releases(self) -> None:
        older_ids = [
            self._add_source(f"Villkor {index}: teckningskurs {index}, garanti och rådgivare.")
            for index in range(1, 6)
        ]
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        item["source_document_ids"] = [self.document_id, *older_ids]

        self.assertTrue(service.process_item(item))
        packet = json.loads(provider.last_kwargs["user_prompt"])["source_packet"]
        self.assertEqual(
            set(packet["document_ids"]), {self.document_id, *older_ids}
        )
        self.assertEqual(len(packet["manifest"]), 6)
        self.assertIn("Villkor", packet["text"])

    def test_directed_cash_outcome_can_be_described_for_announced_status(self) -> None:
        document_id = self._add_source(
            "Editorial AB har beslutat om en riktad emission om 10 MSEK."
        )
        item = self._item(document_id)
        item.update({
            "category": "directed_issue",
            "category_label": "Riktad emission",
            "details": {
                "outcome_amount": 10,
                "outcome_amount_basis": "directed_issue_cash_assumption",
                "completion_ratio_pct": 100.0,
            },
        })
        payload = editorial_payload(
            "Editorial AB: genomförd riktad emission",
            VALID_SUMMARY.replace("företrädesemission", "riktad emission"),
        )
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        self.assertTrue(
            service._valid_payload(
                payload, source, service._facts(item, "transaction:edit-1")
            )
        )
        for invalid_outcome in (None, 0, True):
            item["details"]["outcome_amount"] = invalid_outcome
            self.assertFalse(
                service._valid_payload(
                    payload, source, service._facts(item, "transaction:edit-1")
                )
            )

    def test_directed_cash_rule_is_only_sent_for_directed_issues(self) -> None:
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        rights = self._item()
        rights["details"] = {
            "financing_dossier": {"final_subscription_pct": 1.0},
            "outcome_amount": 10,
            "outcome_amount_basis": "outcome_gross",
        }
        with self.db.connect() as connection:
            rights_source = service._latest_source(connection, rights)
        rights_prompt = json.loads(service._user_prompt(service._facts(rights, rights["id"]), rights_source))
        self.assertNotIn("riktade kontantemissionen", rights_prompt["instruction"])
        self.assertNotIn("kontraktsmarkör", rights_prompt["instruction"])

        directed = dict(rights)
        directed["category"] = "directed_issue"
        directed["details"] = {
            "outcome_amount": 10,
            "outcome_amount_basis": "directed_issue_cash_assumption",
        }
        directed_prompt = json.loads(service._user_prompt(service._facts(directed, directed["id"]), rights_source))
        self.assertIn("riktade kontantemissionen", directed_prompt["instruction"])

    def test_internal_contract_wording_is_rejected(self) -> None:
        payload = editorial_payload("Editorial AB: offentliggjord emission")
        payload["summary"] = "Den deterministiska kontraktsmarkören används. " + payload["summary"]
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        item = self._item()
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        self.assertFalse(service._valid_payload(payload, source, service._facts(item, item["id"])))

    def test_financial_number_validation_accepts_normal_scaling_and_rounding(self) -> None:
        aliases = _evidence_number_aliases("181536223 units and 40.9 percent")
        self.assertIn("181.5", aliases)
        self.assertIn("41", aliases)
        self.assertIn("29", _scaled_fact_numbers({"net_amount": 29_000_000}))
        self.assertIn("34.4", _scaled_fact_numbers({"subscription_pct": 0.344}))
        self.assertIn("59.1", _scaled_fact_numbers({"guarantee_take_up_pct": 0.591}))

    def test_short_model_detail_is_completed_with_source_bound_dossier_facts(self) -> None:
        payload = editorial_payload("Editorial AB: utfall i företrädesemission")
        payload["detail_summary"] = "• Kort självständig analys av affärens huvudsakliga innebörd."
        facts = self._item()
        facts["status"] = "completed"
        facts["details"] = {
            "currency": "SEK",
            "source_count": 6,
            "financing_dossier": {
                "gross_amount": 36_000_000,
                "transaction_cost_total": 7_000_000,
                "net_amount": 29_000_000,
                "subscription_with_rights_pct": 0.344,
                "subscription_without_rights_pct": 0.065,
                "guarantee_take_up_pct_of_issue": 0.591,
                "guarantee_fee_amount": 3_000_000,
                "guarantee_fee_pct": 0.12,
            },
        }
        facts["institutions"] = [
            {"name": "DNB Carnegie", "role": "Finansiell rådgivare"},
            {"name": "Mangold Fondkommission AB", "role": "Garant"},
        ]
        TransactionEditorialService._normalise_payload(payload, facts)
        words = re.findall(r"[\wÅÄÖåäö]+(?:[-'][\wÅÄÖåäö]+)?", payload["detail_summary"])
        self.assertGreaterEqual(len(words), 200)
        self.assertLessEqual(len(words), 400)
        self.assertGreaterEqual(payload["detail_summary"].count("•"), 4)
        self.assertIn("DNB Carnegie", payload["detail_summary"])

    def test_invalid_model_copy_stays_source_grounded(self) -> None:
        provider = _Provider([editorial_payload("Kort", "För kort kommentar.")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        self.assertFalse(service.process_item(item))
        with self.db.connect() as connection:
            result = service.apply_cached_editorial(connection, item)
            self.assertEqual(result["editorial_source"], "source")
            self.assertEqual(result["editorial_status"], "failed")

    def test_invalid_editorial_is_repaired_by_flash_provider(self) -> None:
        primary = _Provider([editorial_payload(
            "Editorial AB: offentliggjord emission med utfall", VALID_SUMMARY
        )])
        repair = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=primary)
        service._enrichment_provider = repair

        self.assertTrue(service.process_item(self._item()))
        self.assertEqual(primary.calls, 1)
        self.assertEqual(repair.calls, 1)
        with self.db.connect() as connection:
            result = service.apply_cached_editorial(connection, self._item())
        self.assertEqual(result["editorial_source"], "ai")
        self.assertEqual(result["editorial_status"], "ready")

    def _add_financing_event(self) -> None:
        with self.db.connect() as connection:
            content_id = connection.execute(
                "SELECT id FROM document_contents WHERE document_id=?", (self.document_id,)
            ).fetchone()["id"]
            job_id = connection.execute(
                """INSERT INTO extraction_jobs
                   (issuer_id, document_id, content_id, provider, model, prompt_version,
                    input_sha256, selected_char_count, estimated_input_tokens, status,
                    attempt_count, started_at, completed_at)
                   VALUES (?, ?, ?, 'test', 'test', 'test', 'hash', 1, 1, 'completed', 1, ?, ?)""",
                (self.issuer_id, self.document_id, content_id, utc_now(), utc_now()),
            ).lastrowid
        _persist_financing_events(
            AnalysisStore(self.db), issuer_id=self.issuer_id, document_id=self.document_id,
            job_id=int(job_id), parsed={"financing_events": [{
                "event_type": "share_issue", "financing_type": "rights_issue",
                "description": "Emission", "status": "announced",
                "cash_direction": "none", "currency": "SEK", "liquidity_effect_kind": "conditional_liquidity",
                "reflected_in_report": "no", "confidence": 0.9,
                "evidence_label": "annonserad företrädesemission",
                "source_excerpt": "Bolaget offentliggjorde en företrädesemission om 10 MSEK.",
            }]},
        )
        with self.db.connect() as connection:
            connection.execute("UPDATE financing_events SET financing_type='rights_issue'")

    def test_post_editorial_enrichment_fills_only_blank_cited_field(self) -> None:
        self._add_financing_event()
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        item = self._item()
        written = service._persist_enrichments(self.issuer_id, item, {"facts": [{
            "field": "transaction_cost", "number_value": 1_300_000,
            "text_value": None, "source_document_id": self.document_id,
            "source_excerpt": "Bolaget offentliggjorde en företrädesemission om 10 MSEK.",
        }]})
        self.assertEqual(written, 1)
        with self.db.connect() as connection:
            row = connection.execute("SELECT transaction_cost FROM financing_events").fetchone()
            audit = connection.execute("SELECT field_name FROM financing_event_enrichment_facts").fetchone()
        self.assertEqual(row["transaction_cost"], 1_300_000)
        self.assertEqual(audit["field_name"], "transaction_cost")
        second_write = service._persist_enrichments(self.issuer_id, item, {"facts": [{
            "field": "transaction_cost", "number_value": 999_999,
            "text_value": None, "source_document_id": self.document_id,
            "source_excerpt": "Bolaget offentliggjorde en företrädesemission om 10 MSEK.",
        }]})
        self.assertEqual(second_write, 0)
        with self.db.connect() as connection:
            preserved = connection.execute("SELECT transaction_cost FROM financing_events").fetchone()
        self.assertEqual(preserved["transaction_cost"], 1_300_000)
        # A release discussing two matching event rows is ambiguous even if
        # only one happens to have an empty field.
        with self.db.connect() as connection:
            connection.execute("UPDATE financing_events SET event_fingerprint='first'")
            columns = [row[1] for row in connection.execute("PRAGMA table_info(financing_events)") if row[1] != "id"]
            expressions = ["'second'" if name == "event_fingerprint" else name for name in columns]
            connection.execute(f"INSERT INTO financing_events ({','.join(columns)}) SELECT {','.join(expressions)} FROM financing_events")
            ids = [row[0] for row in connection.execute("SELECT id FROM financing_events ORDER BY id")]
            evidence_columns = [row[1] for row in connection.execute("PRAGMA table_info(financing_event_evidence)") if row[1] != "id"]
            evidence_values = [str(ids[1]) if name == "financing_event_id" else name for name in evidence_columns]
            connection.execute(f"INSERT INTO financing_event_evidence ({','.join(evidence_columns)}) SELECT {','.join(evidence_values)} FROM financing_event_evidence WHERE financing_event_id=?", (ids[0],))
        ambiguous = service._persist_enrichments(self.issuer_id, item, {"facts": [{
            "field": "guarantee_fee_details", "number_value": None, "text_value": "Garantiersättning 5 procent",
            "source_document_id": self.document_id, "source_excerpt": "Garantiersättning 5 procent",
        }]})
        self.assertEqual(ambiguous, 0)

    def test_evidence_selector_keeps_late_guarantee_clause(self) -> None:
        source = ("Inledning utan emissionsvillkor. " * 250) + (
            "Garantiersättningen uppgår till 12 procent av det garanterade beloppet. "
            "Emissionskostnader beräknas till 1,3 MSEK."
        )
        selected = _select_evidence_sections(source, 1_800)
        self.assertIn("Garantiersättningen", selected)
        self.assertIn("Emissionskostnader", selected)

    def test_evidence_selector_keeps_terms_with_many_linked_sources(self) -> None:
        source = "Inledning utan villkor. " * 250 + "Garantiersättningen är 12 procent. Emissionskostnader är 1,3 MSEK."
        selected = _select_evidence_sections(source, 1500)
        self.assertIn("Garantiersättningen", selected)
        self.assertLessEqual(len(selected), 1500)

    def test_enrichment_checks_document_identity_and_amount_units(self) -> None:
        excerpt = "Emissionskostnaderna uppgår till 1,3 MSEK."
        cost_document = self._add_source(excerpt)
        item = self._item()
        item["source_document_ids"].append(cost_document)
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        entry = {"field": "transaction_cost", "number_value": 1_300_000, "text_value": None,
                 "source_document_id": cost_document, "source_excerpt": excerpt}
        self.assertTrue(service._validate_enrichments({"facts": [entry]}, source, {})[0])
        for change in ({"number_value": 1.3}, {"number_value": float("nan")},
                       {"source_document_id": 999999}):
            self.assertFalse(service._validate_enrichments({"facts": [{**entry, **change}]}, source, {})[0])
        translated_id = {**entry, "source_document_id": self.document_id}
        self.assertTrue(service._validate_enrichments({"facts": [translated_id]}, source, {})[0])
        self.assertEqual(translated_id["source_document_id"], cost_document)

    def test_market_bars_do_not_invalidate_financing_editorial(self) -> None:
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        self.assertTrue(service.process_item(item))
        item["details"] = {"market_reaction": {"average_close_5d": 0.1254}}
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, item)["editorial_source"], "ai")

    def test_second_call_receives_explicit_output_contract(self) -> None:
        service = TransactionEditorialService(self.db, _settings(),
            provider=_Provider([editorial_payload("Editorial AB: offentliggjord emission")]))
        item = self._item()
        self.assertTrue(service.process_item(item))
        flash = _Provider([{"facts": []}])
        service._enrichment_provider = flash
        self.assertFalse(service.enrich_missing_fields(item))
        prompt = json.loads(flash.last_kwargs["user_prompt"])
        self.assertIn("facts", prompt["response_schema"]["properties"])
        self.assertIn("guarantee_fee_details", prompt["allowed_fields"])

    def test_source_backed_second_pass_persists_and_keeps_comment_visible(self) -> None:
        from capital_radar.transaction_feed import get_transaction_feed, get_transaction_detail
        excerpt = "Ersättningen till garanterna är 5 procent kontant."
        self._add_financing_event()
        with self.db.connect() as connection:
            connection.execute("UPDATE documents SET title='Editorial AB offentliggör företrädesemission'")
            connection.execute("UPDATE document_contents SET extracted_text=extracted_text || ?", (" " + excerpt,))
        with self.db.connect() as connection:
            item = get_transaction_feed(connection)["items"][0]
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        self.assertTrue(service.process_item(item))
        flash = _Provider([{"facts": [{"field": "guarantee_fee_details", "number_value": None,
            "text_value": excerpt, "source_document_id": self.document_id, "source_excerpt": excerpt}]}])
        service._enrichment_provider = flash
        self.assertTrue(service.enrich_missing_fields(item))
        with self.db.connect() as connection:
            updated = get_transaction_detail(connection, item["id"])
            self.assertEqual(updated["details"]["financing_dossier"]["guarantee_fee_details"], excerpt)
            self.assertEqual(service.apply_cached_editorial(connection, updated)["editorial_source"], "ai")
            self.assertEqual(connection.execute("SELECT count(*) FROM financing_event_enrichment_facts").fetchone()[0], 1)
            artifact = connection.execute("SELECT content_json,content_sha256 FROM agent_artifacts WHERE agent_type='transaction_editorial'").fetchone()
            self.assertEqual(hashlib.sha256(artifact["content_json"].encode("utf-8")).hexdigest(), artifact["content_sha256"])
        self.assertTrue(service.process_item(updated))
        self.assertEqual(provider.calls, 1)
        self.assertFalse(service.enrich_missing_fields(updated))
        self.assertEqual(flash.calls, 1)

    def test_scaled_deterministic_net_amount_is_valid_editorial_number(self) -> None:
        payload = editorial_payload("Editorial AB: utfall i företrädesemission")
        payload["summary"] = VALID_SUMMARY.replace(
            "för denna sammanställning.",
            "för denna sammanställning. Pro forma-nettot är 29 MSEK efter kostnader.",
        )
        item = self._item()
        item["status"] = "completed"
        item["details"] = {"financing_dossier": {"net_amount": 29_000_000, "transaction_cost_total": 7_000_000}}
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        self.assertTrue(service._valid_payload(payload, source, service._facts(item, item["id"])))

    def test_corrected_source_with_identical_copy_gets_new_binding(self):
        copy = editorial_payload("Editorial AB: offentliggjord emission")
        provider = _Provider([dict(copy), dict(copy)])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        self.assertTrue(service.process_item(item))
        with self.db.connect() as connection:
            connection.execute("UPDATE document_contents SET extracted_text=extracted_text || ' Korrigerad källa.'")
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, item)["editorial_source"], "source")
        self.assertTrue(service.process_item(item))
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, item)["editorial_source"], "ai")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0], 2)
        self.assertEqual(provider.calls, 2)

    def test_changed_facts_invalidate_copy_without_provider_call(self):
        provider = _Provider([editorial_payload("Editorial AB: offentliggjord emission")])
        service = TransactionEditorialService(self.db, _settings(), provider=provider)
        item = self._item()
        self.assertTrue(service.process_item(item))
        item["status"] = "completed"
        item["status_label"] = "Utfall"
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, item)["editorial_source"], "source")
        self.assertEqual(provider.calls, 1)

    def test_unsupported_amount_or_outcome_is_rejected(self):
        for headline, summary in (
            ("Editorial AB: offentliggjord emission", VALID_SUMMARY.replace("10 MSEK", "15 MSEK")),
            ("Editorial AB: offentliggjord emission med utfall", VALID_SUMMARY),
        ):
            with self.subTest(headline=headline):
                service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
                with self.db.connect() as connection:
                    source = service._latest_source(connection, self._item())
                self.assertFalse(service._valid_payload(editorial_payload(headline, summary), source, service._facts(self._item(), "transaction:edit-1")))

    def test_unsupported_long_form_number_is_removed_without_losing_editorial(self):
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        item = self._item()
        payload = editorial_payload("Editorial AB: offentliggjord emission")
        payload["detail_summary"] += " Bolaget emitterar 9510983 nya aktier."
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        valid, _, _ = service._validate(payload, source, service._facts(item, item["id"]))
        self.assertTrue(valid)
        self.assertNotIn("9510983", payload["detail_summary"])

    def test_absent_key_disables_service_and_preserves_source(self) -> None:
        service = TransactionEditorialService(self.db, _settings())
        self.assertFalse(service.enabled)
        item = self._item()
        self.assertFalse(service.process_item(item))

    def test_oversized_summary_is_shortened_at_sentence_boundary(self):
        payload = editorial_payload("Editorial AB: offentliggjord emission", summary=" ".join([VALID_SUMMARY] * 3))
        service = TransactionEditorialService(self.db, _settings(), provider=_Provider([]))
        item = self._item()
        with self.db.connect() as connection:
            source = service._latest_source(connection, item)
        self.assertTrue(service._validate(payload, source, service._facts(item, item["id"]))[0])
        self.assertLessEqual(len(payload["summary"].split()), 110)
        self.assertTrue(payload["summary"].endswith('.'))
        with self.db.connect() as connection:
            self.assertEqual(service.apply_cached_editorial(connection, item)["summary"], item["summary"])


if __name__ == "__main__":
    unittest.main()
