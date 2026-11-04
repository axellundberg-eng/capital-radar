"""Targeted tester för V2-extraction (openai + instructor + pydantic).

Dessa tester mockar Instructor/AsyncOpenAI så att inga riktiga provider-anrop
görs. De verifierar stabilization-policyn: 1 job = MAX 1 faktisk request,
quality_gate klassificerar bara (aldrig nytt anrop), fel klassificeras som
validation_failed/timeout/rate_limited/failed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from capital_radar.extraction_models import (
    CapitalNeedExtraction,
    FinancialFact,
    FinancingEvent,
    FactType,
)
from capital_radar.analysis_store import AnalysisStore
from capital_radar.db import Database
from capital_radar.extraction_v2 import (
    PROMPT_VERSION_V2,
    SYSTEM_PROMPT_V2,
    _record_run,
    _persist_financing_events,
    _usage_dict,
    _user_prompt_v2,
    run_extraction_v2,
)


def _make_extraction(**overrides: Any) -> CapitalNeedExtraction:
    base: dict[str, Any] = {
        "document_class": "financial_report",
        "document_summary": "Delårsrapport.",
        "report": {
            "is_financial_report": True,
            "currency": "SEK",
            "confidence": 0.9,
            "facts": [
                {
                    "fact_type": "cash_and_cash_equivalents",
                    "reported_value": 100.0,
                    "normalized_value": 100000.0,
                    "currency": "SEK",
                    "reported_unit": "thousands",
                    "evidence_label": "reported_fact",
                    "source_page": 3,
                    "source_excerpt": "Kassa och bank 100",
                    "confidence": 0.95,
                },
                {
                    "fact_type": "operating_cash_flow",
                    "reported_value": -50.0,
                    "normalized_value": -50000.0,
                    "currency": "SEK",
                    "reported_unit": "thousands",
                    "evidence_label": "reported_fact",
                    "source_page": 4,
                    "source_excerpt": "Kassaflöde från den löpande verksamheten -50",
                    "confidence": 0.9,
                },
            ],
        },
        "financing_events": [],
        "quality": {
            "complete": True,
            "uncertainties": [],
            "requires_human_review": False,
        },
    }
    base.update(overrides)
    return CapitalNeedExtraction.model_validate(base)


def _completion(usage: Any = None, request_id: str = "req-1") -> MagicMock:
    completion = MagicMock()
    completion.id = request_id
    completion.usage = usage
    completion.finish_reason = "stop"
    return completion


def _usage(prompt: int = 1000, completion: int = 500) -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.total_tokens = prompt + completion
    usage.prompt_tokens_details = None
    return usage


class _FakeCandidate:
    issuer_id = 103
    document_id = 55
    issuer_name = "Xspray Pharma"
    title = "Delårsrapport Q3"
    published_at = "2026-05-01"
    source_url = "https://example.com/report.pdf"


class _FakeSelection:
    text = "Kassa och bank 100 tSEK. Kassaflöde från den löpande verksamheten -50 tSEK."
    selected_pages = [3, 4]
    selected_char_count = 80
    estimated_tokens = 40


class _FakeStore:
    """Minimal store som fångar complete/fail och agent_runs/artifacts."""

    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[str] = []
        self.db = MagicMock()
        self.db.connect.return_value.__enter__ = lambda s: s
        self.db.connect.return_value.__exit__ = MagicMock(return_value=False)
        self.db.connect.return_value.execute.side_effect = self._execute

    @staticmethod
    def _execute(sql: str, params: Any = None):  # noqa: ANN001
        cursor = MagicMock()
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("SELECT"):
            # Inga dubbletter / version_no-start → fetchone() = None eller (0,).
            if "COALESCE(MAX(VERSION_NO)" in normalized:
                cursor.fetchone.return_value = [0]
            else:
                cursor.fetchone.return_value = None
        else:
            # INSERT ... RETURNING id
            cursor.fetchone.return_value = [1]
            cursor.lastrowid = 7
        return cursor

    def complete_job(self, job_id, lease_token, **kwargs):  # noqa: ANN001
        self.completed.append(kwargs)
        return True

    def fail_job(self, job_id, lease_token, error, status="failed"):  # noqa: ANN001
        self.failed.append(error)
        return True


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.deepseek_api_key = "sk-test"
    settings.deepseek_base_url = "https://api.deepseek.com"
    settings.deepseek_flash_model = "deepseek-v4-flash"
    settings.deepseek_pro_model = "deepseek-v4-pro"
    settings.deepseek_flash_max_output_tokens = 8192
    settings.deepseek_pro_extraction_max_output_tokens = 16000
    settings.deepseek_pro_max_output_tokens = 8192
    settings.deepseek_flash_read_timeout_seconds = 120.0
    settings.deepseek_pro_read_timeout_seconds = 300.0
    return settings


def test_pydantic_validation_success() -> None:
    model = _make_extraction()
    dumped = model.model_dump(mode="json")
    assert set(dumped) == {
        "document_class",
        "document_summary",
        "report",
        "financing_events",
        "quality",
    }
    assert "bergs_fit" not in dumped
    assert dumped["report"]["facts"][0]["fact_type"] == "cash_and_cash_equivalents"


def test_pydantic_ignores_unknown_field() -> None:
    # DeepSeek hallucinerar ibland extra fält (t.ex. source_excerpt_note,
    # normalized_gross). Med max_retries=0 får det inte krascha extraction —
    # extra="ignore" gör att okända fält tyst ignoreras istället för att
    # avvisas. De sparas inte i modellen.
    model = CapitalNeedExtraction.model_validate({"unknown_key": 1})
    assert "unknown_key" not in model.model_dump()


def test_pydantic_ignores_nested_unknown_fields() -> None:
    # Verifierar att extra fält på nested-nivå (där prod-felet uppstod)
    # ignoreras utan att krascha.
    model = CapitalNeedExtraction.model_validate(
        {
            "report": {
                "is_financial_report": True,
                "facts": [
                    {
                        "fact_type": "capex",
                        "source_excerpt_note": "hallucinerat fält",
                    }
                ],
            },
            "financing_events": [
                {"event_type": "directed_issue", "normalized_gross": -13000}
            ],
        }
    )
    assert "source_excerpt_note" not in model.report.facts[0].model_dump()
    assert "normalized_gross" not in model.financing_events[0].model_dump()


def test_financing_event_accepts_source_backed_issue_terms() -> None:
    event = FinancingEvent.model_validate(
        {
            "event_type": "rights_issue",
            "canonical_round_key": "issuer-42-rights-2026-09",
            "lifecycle_stage": "terms_published",
            "issue_price": 1.25,
            "subscription_commitment_amount": 4_000_000,
            "guarantee_commitment_amount": 4_000_000,
            "guarantee_fee_details": "Top guarantee: 12 percent cash fee.",
            "transaction_cost": 750_000,
            "adviser": "Example Capital",
        }
    )
    assert event.lifecycle_stage == "terms_published"
    assert event.guarantee_commitment_amount == 4_000_000
    assert event.transaction_cost == 750_000


def test_prompt_requires_source_backed_financing_lifecycle_and_costs() -> None:
    assert "canonical_round_key" in SYSTEM_PROMPT_V2
    assert "guarantee compensation" in SYSTEM_PROMPT_V2
    assert "Do not infer fees" in SYSTEM_PROMPT_V2


def test_financing_terms_are_persisted_by_v2_bridge(tmp_path) -> None:
    db = Database(tmp_path / "extraction.db")
    db.initialize()
    with db.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO issuers (canonical_name, normalized_name, created_at, updated_at)
               VALUES ('Example AB', 'example ab', '2026-09-10', '2026-09-10')"""
        )
        issuer_id = int(cursor.lastrowid)
    event = FinancingEvent.model_validate(
        {
            "event_type": "rights_issue",
            "description": "Företrädesemission om 10 MSEK",
            "announcement_date": "2026-09-10",
            "status": "announced",
            "cash_direction": "none",
            "currency": "SEK",
            "liquidity_effect_kind": "conditional_liquidity",
            "canonical_round_key": "example-rights-2026-09",
            "lifecycle_stage": "terms_published",
            "issue_price": 1.25,
            "subscription_commitment_amount": 4_000_000,
            "guarantee_commitment_amount": 4_000_000,
            "guarantee_fee_details": "Top guarantee: 12 percent cash fee.",
            "transaction_cost": 750_000,
            "adviser": "Example Capital",
        }
    ).model_dump(mode="json")
    written = _persist_financing_events(
        AnalysisStore(db),
        issuer_id=issuer_id,
        document_id=None,
        job_id=1,
        parsed={"financing_events": [event]},
    )
    assert written == 1
    with db.connect() as connection:
        row = connection.execute(
            """SELECT canonical_round_key, lifecycle_stage, issue_price,
                      subscription_commitment_amount, guarantee_commitment_amount,
                      guarantee_fee_details, transaction_cost, adviser
                 FROM financing_events WHERE issuer_id=?""",
            (issuer_id,),
        ).fetchone()
    assert row["canonical_round_key"] == "example-rights-2026-09"
    assert row["lifecycle_stage"] == "terms_published"
    assert row["subscription_commitment_amount"] == 4_000_000
    assert row["guarantee_fee_details"] == "Top guarantee: 12 percent cash fee."


def test_pydantic_rejects_invalid_enum() -> None:
    with pytest.raises(ValidationError):
        FinancialFact.model_validate(
            {"fact_type": "ebitda"}  # inte en giltig FactType
        )


def test_usage_dict_none() -> None:
    assert _usage_dict(None) == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_tokens": None,
        "total_tokens": None,
    }


def test_flash_success_completes_job() -> None:
    store = _FakeStore()
    model = _make_extraction()
    completion = _completion(usage=_usage())

    calls = {"n": 0}

    async def _fake_attempt(*args: Any, **kwargs: Any):
        calls["n"] += 1
        # _extract_async returnerar (model, completion) — latency mäts i _attempt.
        return model, completion

    with patch(
        "capital_radar.extraction_v2.build_async_client"
    ) as build, patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        build.return_value = MagicMock()
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "completed"
    assert outcome["model"] == "deepseek-v4-flash"
    assert calls["n"] == 1, "success = exakt 1 request"
    assert outcome["provider_calls"] == 1
    assert store.completed, "complete_job ska ha anropats"
    parsed = store.completed[0]["parsed_output"]
    assert parsed["report"]["facts"], "facts ska finnas i persisted output"
    assert "bergs_fit" not in parsed
    assert outcome["usage"]["prompt_tokens"] == 1000


def test_financing_event_persist_failure_keeps_extraction_completed() -> None:
    # Regression: undantagshanteraren refererade ett odefinierat `logger`,
    # så ett skrivfel för finansieringshändelser fällde hela extractionen.
    store = _FakeStore()
    model = _make_extraction()
    completion = _completion(usage=_usage())

    async def _fake_attempt(*args: Any, **kwargs: Any):
        return model, completion

    with patch(
        "capital_radar.extraction_v2.build_async_client"
    ) as build, patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ), patch(
        "capital_radar.extraction_v2._persist_financing_events",
        side_effect=RuntimeError("disk full"),
    ) as persist:
        build.return_value = MagicMock()
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert persist.called
    assert outcome["status"] == "completed"
    assert store.completed


def test_schema_error_is_validation_failed_single_request() -> None:
    """Schemafel → validation_failed, 1 request, INGEN Pro-eskalation."""
    store = _FakeStore()

    calls = {"n": 0}

    async def _fake_attempt(*args: Any, **kwargs: Any):
        calls["n"] += 1
        raise ValidationError.from_exception_data("CapitalNeedExtraction", [])

    with patch("capital_radar.extraction_v2.build_async_client"), patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "validation_failed"
    assert outcome["error_type"] == "schema_validation"
    assert calls["n"] == 1, "schemafel = 1 request, aldrig Pro"
    assert outcome["provider_calls"] == 1
    assert not store.completed
    assert store.failed, "fail_job ska ha anropats"


def test_provider_error_is_failed_single_request_no_pro() -> None:
    """Providerfel → failed, 1 request, INGEN Pro-eskalation."""
    store = _FakeStore()

    calls = {"n": 0}

    async def _fake_attempt(*args: Any, **kwargs: Any):
        calls["n"] += 1
        raise RuntimeError("provider nere")

    with patch("capital_radar.extraction_v2.build_async_client"), patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "failed"
    assert outcome["error_type"] == "provider"
    assert calls["n"] == 1, "Flash-fel = 1 request, ingen Pro"
    assert outcome["provider_calls"] == 1
    assert not store.completed


def test_quality_gate_escalate_does_not_trigger_new_request() -> None:
    """quality_gate=escalate klassificerar bara — aldrig ett nytt AI-anrop."""
    store = _FakeStore()
    # Tom fakta-lista → central_facts_missing → GATE_ESCALATE.
    model = _make_extraction(report={"is_financial_report": True, "facts": []})
    completion = _completion(usage=_usage())

    calls = {"n": 0}

    async def _fake_attempt(*args: Any, **kwargs: Any):
        calls["n"] += 1
        return model, completion

    with patch("capital_radar.extraction_v2.build_async_client"), patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "completed"
    assert outcome["gate_decision"] == "escalate"
    assert calls["n"] == 1, "gate-escalation får aldrig trigga request #2"
    assert outcome["provider_calls"] == 1
    parsed = store.completed[0]["parsed_output"]
    assert parsed["quality"]["gate_decision"] == "escalate"


def test_timeout_classified_as_timeout() -> None:
    store = _FakeStore()

    async def _fake_attempt(*args: Any, **kwargs: Any):
        raise asyncio.TimeoutError()

    with patch("capital_radar.extraction_v2.build_async_client"), patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "timeout"
    assert outcome["error_type"] == "timeout"
    assert outcome["provider_calls"] == 1


def test_rate_limit_classified_as_rate_limited() -> None:
    from openai import RateLimitError

    store = _FakeStore()

    async def _fake_attempt(*args: Any, **kwargs: Any):
        request = MagicMock()
        response = MagicMock()
        response.status_code = 429
        response.headers = {}
        raise RateLimitError("rate limited", response=response, body=None)

    with patch("capital_radar.extraction_v2.build_async_client"), patch(
        "capital_radar.extraction_v2.instructor.from_openai"
    ), patch(
        "capital_radar.extraction_v2._extract_async", side_effect=_fake_attempt
    ):
        outcome = run_extraction_v2(
            store,
            _settings(),
            candidate=_FakeCandidate(),
            selection=_FakeSelection(),
            job_id=1,
            lease_token="lease",
        )
    assert outcome["status"] == "rate_limited"
    assert outcome["error_type"] == "rate_limit"
    assert outcome["provider_calls"] == 1


def test_user_prompt_v2_report_hint_for_reports() -> None:
    """Rapporter (profile_category='report') ska få en classification-hint så
    att modellen sätter is_financial_report=true. Annars kan en delårsrapport
    med marknadsförande titel missklassificeras och aldrig ge runway."""

    class _Report:
        issuer_name = "4C Group"
        title = "A step in the right direction – Interim Report, January-June 2026"
        published_at = "2026-08-25"
        source_url = "https://example.com/q2.pdf"
        profile_category = "report"

    prompt = _user_prompt_v2(_Report(), "Dokumenttext")
    assert "periodic financial report" in prompt
    assert "is_financial_report=true" in prompt


def test_user_prompt_v2_no_hint_for_non_reports() -> None:
    """Icke-rapporter (t.ex. pressmeddelanden) ska INTE få hinten."""

    class _PressRelease:
        issuer_name = "4C Group"
        title = "4C Strategies signs contract"
        published_at = "2026-08-12"
        source_url = "https://example.com/pm.pdf"
        profile_category = "commercial_deal"

    prompt = _user_prompt_v2(_PressRelease(), "Dokumenttext")
    assert "periodic financial report" not in prompt


def test_oversubscribed_rights_issue_maps_to_confirmed_cash() -> None:
    """Ett genomfört emissionsutfall ska kunna bli confirmed_cash/completed.

    Regression för 4C Group: utfalls-PM:et ("kraftigt övertecknad – bolaget
    beslutar om utnyttjande av övertilldelningsoptionen") lämnade tidigare
    cash_effect_amount=null → aldrig confirmed_cash → runway justerades aldrig.
    Modellen ska acceptera ett completed confirmed_cash-event med bruttobelopp.
    """
    from capital_radar.extraction_models import (
        CapitalNeedExtraction,
        EventStatus,
        LiquidityEffectKind,
        ReflectedInReport,
    )

    model = CapitalNeedExtraction.model_validate(
        {
            "document_class": "financing_announcement",
            "document_summary": "Emissionsutfall.",
            "financing_events": [
                {
                    "event_type": "rights_issue",
                    "description": (
                        "Företrädesemissionen kraftigt övertecknad; bolaget "
                        "tillförs cirka 74 MSEK före emissionkostnader."
                    ),
                    "announcement_date": "2026-07-24",
                    "effective_date": "2026-07-24",
                    "status": "completed",
                    "cash_direction": "inflow",
                    "gross_amount": 74.0,
                    "net_amount": None,
                    "cash_effect_amount": 74.0,
                    "currency": "SEK",
                    "liquidity_effect_kind": "confirmed_cash",
                    "reflected_in_report": "no",
                    "confidence": 0.6,
                    "source_page": 1,
                    "source_excerpt": "bolaget tillförs cirka 74 MSEK",
                }
            ],
            "quality": {"requires_human_review": True},
        }
    )
    event = model.financing_events[0]
    assert event.status == EventStatus.COMPLETED
    assert event.liquidity_effect_kind == LiquidityEffectKind.CONFIRMED_CASH
    assert event.cash_effect_amount == 74.0
    assert event.net_amount is None
    assert event.reflected_in_report == ReflectedInReport.NO
    assert model.quality.requires_human_review is True


def test_system_prompt_treats_oversubscription_as_completed() -> None:
    """Prompten ska instruera att ett övertecknings-/utfalls-PM räknas som
    genomförd finansiering (annars blir cash_effect_amount alltid null)."""
    assert "oversubscribed" in SYSTEM_PROMPT_V2
    assert "status=completed" in SYSTEM_PROMPT_V2
    assert "confirmed_cash" in SYSTEM_PROMPT_V2
    assert "reflected_in_report=no" in SYSTEM_PROMPT_V2


def test_idempotency_key_stable() -> None:
    from capital_radar.hashing import (
        compute_idempotency_key,
        compute_input_hash,
    )

    h1 = compute_input_hash(
        agent_type="financial_extraction",
        prompt_version=PROMPT_VERSION_V2,
        system_prompt=SYSTEM_PROMPT_V2,
        user_prompt="abc",
        model="deepseek-v4-flash",
    )
    h2 = compute_input_hash(
        agent_type="financial_extraction",
        prompt_version=PROMPT_VERSION_V2,
        system_prompt=SYSTEM_PROMPT_V2,
        user_prompt="abc",
        model="deepseek-v4-flash",
    )
    assert h1 == h2
    k1 = compute_idempotency_key(
        agent_type="financial_extraction",
        issuer_id=103,
        case_id=None,
        input_hash=h1,
    )
    k2 = compute_idempotency_key(
        agent_type="financial_extraction",
        issuer_id=103,
        case_id=None,
        input_hash=h2,
    )
    assert k1 == k2


def test_record_run_insert_matches_schema() -> None:
    """Regression: _record_run INSERT måste matcha agent_runs-schemat exakt.

    Tidigare bugg: 27 placeholders för 28 kolumner gav 'N values for M
    columns' i produktion. Kör mot riktig in-memory SQLite så att kolumn-
    och placeholder-antal alltid valideras.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT, issuer_id INTEGER, deal_id INTEGER,
            agent_type TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
            thinking_enabled INTEGER NOT NULL DEFAULT 0, reasoning_effort TEXT,
            prompt_version TEXT NOT NULL, schema_version TEXT NOT NULL DEFAULT 'v1',
            execution_profile_version TEXT NOT NULL DEFAULT 'v1',
            execution_profile_json TEXT NOT NULL DEFAULT '{}',
            input_hash TEXT NOT NULL, source_data_cutoff TEXT, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT, latency_ms INTEGER,
            input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
            estimated_cost_usd REAL, retry_count INTEGER NOT NULL DEFAULT 0,
            provider_request_id TEXT, confidence REAL, artifact_id INTEGER,
            error_type TEXT, safe_error_message TEXT,
            idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(idempotency_key)
        )
        """
    )

    class _Conn:
        def __enter__(self):  # noqa: ANN204
            return conn

        def __exit__(self, *a):  # noqa: ANN001
            conn.commit()
            return False

    class _DB:
        def connect(self):  # noqa: ANN204
            return _Conn()

    store = MagicMock()
    store.db = _DB()
    profile = MagicMock()
    profile.name = "flash_extraction"
    profile.model = "deepseek-v4-flash"
    profile.thinking = False
    profile.reasoning_effort = None
    profile.max_output_tokens = 8192

    run_id = _record_run(
        store,
        idem_key="key-1",
        profile=profile,
        input_hash="hash",
        issuer_id=103,
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        latency_ms=12,
        usage={"prompt_tokens": 10, "cached_tokens": 0, "completion_tokens": 5},
        request_id="req",
        artifact_id=7,
    )
    assert run_id == 1
    row = conn.execute(
        "SELECT issuer_id, status, input_tokens, output_tokens FROM agent_runs"
    ).fetchone()
    assert row == (103, "completed", 10, 5)


def test_normalize_amount_converts_thousands():
    """Belopp under 100 000 ska normaliseras enligt intervall."""
    from capital_radar.extraction_v2 import _normalize_amount

    # < 1 000: multiplicera med 1 000 000 (MSEK-fel)
    assert _normalize_amount(5) == 5_000_000
    assert _normalize_amount(250) == 250_000_000
    assert _normalize_amount(999) == 999_000_000

    # < 10 000: multiplicera med 100 000 (100kSEK-fel)
    assert _normalize_amount(2_000) == 200_000_000
    assert _normalize_amount(9_999) == 999_900_000

    # < 100 000: multiplicera med 1 000 (TSEK-fel)
    assert _normalize_amount(15_000) == 15_000_000
    assert _normalize_amount(84_287) == 84_287_000
    assert _normalize_amount(99_999) == 99_999_000

    # Negativa belopp (t.ex. debt_repayment)
    assert _normalize_amount(-5) == -5_000_000
    assert _normalize_amount(-9_617) == -961_700_000  # < 10 000 -> * 100 000

    # Redan korrekta belopp (>= 100 000) ska inte ändras
    assert _normalize_amount(100_000) == 100_000
    assert _normalize_amount(445_000) == 445_000
    assert _normalize_amount(500_000) == 500_000
    assert _normalize_amount(1_000_000) == 1_000_000
    assert _normalize_amount(84_287_000) == 84_287_000

    # None ska förbli None
    assert _normalize_amount(None) is None

    # Noll ska förbli noll (inte multipliceras)
    assert _normalize_amount(0) == 0
