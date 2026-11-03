"""Tester för den oberoende granskaren. Inga riktiga API-anrop: en fejkad
Anthropic-klient spelar upp förberedda svar och räknar anropen."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from capital_radar.config import Settings
from capital_radar.db import Database
from capital_radar.reviewer import prompts
from capital_radar.reviewer.providers import AnthropicReviewProvider, build_provider
from capital_radar.reviewer.service import ReviewService, build_review_service

NOW = "2026-11-03T10:00:00+00:00"
PAGE_3 = (
    "Koncernens likvida medel uppgick per den 30 september till 12 345 TSEK. "
    "Kassaflödet från den löpande verksamheten var -4 100 TSEK."
)
PAGE_5 = (
    "Bolaget har tecknat avtal om en kreditfacilitet om 20 MSEK. "
    "Faciliteten är outnyttjad per rapportdagen."
)
DEEPSEEK_TABLES = (
    "issuers", "documents", "document_contents", "extraction_jobs",
    "financial_facts", "financing_events", "financing_event_evidence",
    "document_evidence_texts", "document_evidence_pages",
)


class FakeMessages:
    def __init__(self, responses: list[Any], delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClient:
    def __init__(self, responses: list[Any], delay: float = 0.0) -> None:
        self.messages = FakeMessages(responses, delay)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _message(payload: Any, *, stop_reason: str = "end_turn") -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(
        id="msg_test",
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(input_tokens=2100, output_tokens=640, cache_read_input_tokens=0),
    )


def _settings(tmp_path: Path, **overrides: Any) -> Any:
    values = {
        "db_path": tmp_path / "screener.db",
        "anthropic_api_key": "test-key",
        "reviewer_model": "claude-haiku-5-5",
        "reviewer_effort": "medium",
        "reviewer_max_output_tokens": 8000,
        "reviewer_connect_timeout_seconds": 1.0,
        "reviewer_read_timeout_seconds": 2.0,
        "reviewer_hard_timeout_seconds": 3.0,
        "reviewer_max_source_chars": 24000,
        "reviewer_headline_batch_size": 40,
    }
    values.update(overrides)
    values["reviewer_enabled"] = bool(values["anthropic_api_key"])
    return SimpleNamespace(**values)


def _seed(db: Database) -> dict[str, int]:
    with db.connect() as c:
        issuer = c.execute(
            "INSERT INTO issuers (canonical_name, normalized_name, created_at, updated_at) "
            "VALUES ('Testbolaget AB', 'testbolaget', ?, ?)", (NOW, NOW),
        ).lastrowid
        doc = c.execute(
            """INSERT INTO documents (issuer_id, event_key, canonical_url, title,
               published_at, cash_relevance_score, relevance_reason,
               queued_for_analysis, created_at, updated_at)
               VALUES (?, 'ev1', 'https://mfn.se/a', 'Delårsrapport Q3 2026', ?, 80,
               'rapport', 1, ?, ?)""", (issuer, NOW, NOW, NOW),
        ).lastrowid
        content = c.execute(
            "INSERT INTO document_contents (document_id, source_url, status, created_at, updated_at) "
            "VALUES (?, 'https://mfn.se/a.pdf', 'purged', ?, ?)", (doc, NOW, NOW),
        ).lastrowid
        parsed = {
            "document_class": "financial_report",
            "document_summary": "Delårsrapport.",
            "report": {"is_financial_report": True, "currency": "SEK",
                       "reported_scale": "thousands", "period_end": "2026-09-30"},
            "quality": {"uncertainties": []},
        }
        job = c.execute(
            """INSERT INTO extraction_jobs (issuer_id, document_id, content_id, provider,
               model, prompt_version, input_sha256, selected_char_count,
               estimated_input_tokens, status, parsed_output_json, started_at, completed_at)
               VALUES (?, ?, ?, 'deepseek', 'deepseek-v4-flash', 'capital-need-extraction-v3',
               'abc', 1000, 300, 'completed', ?, ?, ?)""",
            (issuer, doc, content, json.dumps(parsed), NOW, NOW),
        ).lastrowid
        fact = c.execute(
            """INSERT INTO financial_facts (issuer_id, document_id, extraction_job_id,
               fact_type, reported_value, normalized_value, currency, reported_unit,
               as_of_date, evidence_label, source_page, source_excerpt, confidence, created_at)
               VALUES (?, ?, ?, 'cash_and_cash_equivalents', 12345, 12345000, 'SEK',
               'thousands', '2026-09-30', 'reported_fact', 3,
               'likvida medel uppgick per den 30 september till 12 345 TSEK', 0.9, ?)""",
            (issuer, doc, job, NOW),
        ).lastrowid
        event = c.execute(
            """INSERT INTO financing_events (issuer_id, event_fingerprint, event_type,
               description, status, cash_direction, gross_amount, cash_effect_amount,
               currency, liquidity_effect_kind, reflected_in_report, confidence,
               created_at, updated_at)
               VALUES (?, 'fp1', 'credit_facility', 'Kreditfacilitet 20 MSEK', 'completed',
               'inflow', 20000000, 20000000, 'SEK', 'confirmed_cash', 'yes', 0.8, ?, ?)""",
            (issuer, NOW, NOW),
        ).lastrowid
        c.execute(
            """INSERT INTO financing_event_evidence (financing_event_id, document_id,
               extraction_job_id, evidence_label, source_page, source_excerpt, created_at)
               VALUES (?, ?, ?, 'reported_fact', 5,
               'tecknat avtal om en kreditfacilitet om 20 MSEK', ?)""",
            (event, doc, job, NOW),
        )
        full_text = f"[PAGE 3]\n{PAGE_3}\n[PAGE 4]\nIrrelevant sida.\n[PAGE 5]\n{PAGE_5}"
        evidence = c.execute(
            """INSERT INTO document_evidence_texts (document_id, content_id, text_sha256,
               full_text, char_count, page_count, created_at)
               VALUES (?, ?, 'sha', ?, ?, 3, ?)""",
            (doc, content, full_text, len(full_text), NOW),
        ).lastrowid
        for number, text in ((3, PAGE_3), (4, "Irrelevant sida."), (5, PAGE_5)):
            c.execute(
                "INSERT INTO document_evidence_pages VALUES (?, ?, 0, ?, ?)",
                (evidence, number, len(text), text),
            )
    return {"issuer": issuer, "document": doc, "job": job, "fact": fact, "event": event}


def _good_review(ids: dict[str, int]) -> dict[str, Any]:
    return {
        "items": [
            {
                "target_kind": "financial_fact", "target_id": ids["fact"],
                "verdict": "confirmed", "issues": [],
                "reasoning": "Beloppet och enheten stämmer.",
                "source_quote": "likvida medel uppgick per den 30 september till 12 345 TSEK",
                "expected_value": None,
            },
            {
                "target_kind": "financing_event", "target_id": ids["event"],
                "verdict": "disputed",
                "issues": ["liquidity_effect_kind", "loan_counted_as_cash", "reflected_in_report"],
                "reasoning": "Outnyttjad kreditfacilitet är inte kassa.",
                "source_quote": "Faciliteten är outnyttjad per rapportdagen.",
                "expected_value": "committed_liquidity, reflected_in_report=no",
            },
        ],
        "overall_comment": "Facilitet felaktigt räknad som kassa.",
    }


def _service(tmp_path: Path, responses: list[Any], **overrides: Any):
    settings = _settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    db.initialize()
    ids = _seed(db)
    client = FakeClient(responses)
    service = ReviewService(
        db, settings, provider=AnthropicReviewProvider(settings, client=client),
        clock=lambda: NOW,
    )
    return service, db, ids, client


def _snapshot(db: Database) -> dict[str, list[tuple]]:
    with db.connect() as c:
        return {
            table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in DEEPSEEK_TABLES
        }


def _count(db: Database, table: str) -> int:
    with db.connect() as c:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_review_stores_verdicts_and_sends_structured_request(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    client.messages.responses.append(_message(_good_review(ids)))
    result = service.run(prompts.EXTRACTION_TASK, limit=5)

    assert result["counts"] == {"completed": 1}
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    # Bara citerade sidor (3 och 5), inte hela dokumentet.
    user = call["messages"][0]["content"]
    assert "[SIDA 3]" in user and "[SIDA 5]" in user and "Irrelevant sida" not in user

    with db.connect() as c:
        run = c.execute("SELECT * FROM review_runs").fetchone()
        verdicts = c.execute(
            "SELECT target_kind, verdict, issues_json, expected_value, quote_found_in_source, "
            "model, prompt_version FROM review_verdicts ORDER BY target_kind"
        ).fetchall()
    assert run["status"] == "completed"
    assert run["input_tokens"] == 2100 and run["output_tokens"] == 640
    assert run["extraction_job_id"] == ids["job"]
    assert [v["verdict"] for v in verdicts] == ["confirmed", "disputed"]
    assert "loan_counted_as_cash" in json.loads(verdicts[1]["issues_json"])
    assert verdicts[1]["expected_value"].startswith("committed_liquidity")
    assert all(v["quote_found_in_source"] == 1 for v in verdicts)
    assert verdicts[0]["model"] == "claude-haiku-5-5"
    assert verdicts[0]["prompt_version"] == prompts.EXTRACTION_PROMPT_VERSION


def test_storage_is_idempotent(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    client.messages.responses.append(_message(_good_review(ids)))
    prepared = service.prepare_extraction_reviews(limit=5)
    assert len(prepared) == 1

    first = asyncio.run(service.execute(prepared[0]))
    again = asyncio.run(service.execute(prepared[0]))
    assert first["status"] == "completed"
    assert again["status"] == "skipped_duplicate"
    assert len(client.messages.calls) == 1
    assert _count(db, "review_runs") == 1
    assert _count(db, "review_verdicts") == 2
    # Jobbet listas inte längre som ogranskat.
    assert service.prepare_extraction_reviews(limit=5) == []


def test_deepseek_data_is_byte_identical_after_review(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    client.messages.responses.append(_message(_good_review(ids)))
    before = _snapshot(db)
    result = service.run(prompts.EXTRACTION_TASK, limit=5)
    assert result["counts"] == {"completed": 1}
    assert _snapshot(db) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"wrong": "schema"},
        "inte json alls",
        {"items": [{"target_kind": "financial_fact", "target_id": 1, "verdict": "kanske",
                    "issues": [], "reasoning": "x", "source_quote": ""}],
         "overall_comment": ""},
    ],
)
def test_wrong_schema_is_validation_failed(tmp_path: Path, payload: Any) -> None:
    service, db, ids, client = _service(tmp_path, [_message(payload)])
    before = _snapshot(db)
    result = service.run(prompts.EXTRACTION_TASK, limit=5)
    assert result["counts"] == {"validation_failed": 1}
    with db.connect() as c:
        run = c.execute("SELECT status, error_type, raw_output FROM review_runs").fetchone()
    assert run["status"] == "validation_failed"
    assert run["error_type"] == "schema_validation"
    assert run["raw_output"]  # råsvaret sparas för felsökning
    assert _count(db, "review_verdicts") == 0
    assert _snapshot(db) == before
    # Ingen ny granskning av samma indata, inte ens med --retry-failed.
    assert service.prepare_extraction_reviews(limit=5, retry_failed=True) == []


def test_unknown_target_id_is_validation_failed(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    review = _good_review(ids)
    review["items"][0]["target_id"] = 9999
    client.messages.responses.append(_message(review))
    result = service.run(prompts.EXTRACTION_TASK, limit=5)
    assert result["counts"] == {"validation_failed": 1}
    assert _count(db, "review_verdicts") == 0


def test_missing_verdict_is_incomplete_and_truncation_fails(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    review = _good_review(ids)
    review["items"] = review["items"][:1]
    client.messages.responses.append(_message(review))
    assert service.run(prompts.EXTRACTION_TASK, limit=5)["counts"] == {"incomplete": 1}
    assert _count(db, "review_verdicts") == 1


def test_max_tokens_stop_is_validation_failed(tmp_path: Path) -> None:
    service, db, ids, client = _service(
        tmp_path, [_message('{"items": [', stop_reason="max_tokens")]
    )
    assert service.run(prompts.EXTRACTION_TASK, limit=5)["counts"] == {"validation_failed": 1}


def test_missing_key_disables_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # ingen lokal .env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SCREENER_DB_PATH", str(tmp_path / "screener.db"))
    settings = Settings.from_env()
    assert settings.reviewer_enabled is False
    assert build_provider(settings) is None

    db = Database(settings.db_path)
    db.initialize()
    _seed(db)
    service = build_review_service(settings, db=db)
    result = service.run(prompts.EXTRACTION_TASK, limit=5)
    assert result["status"] == "disabled"
    assert "ANTHROPIC_API_KEY" in result["reason"]
    assert _count(db, "review_runs") == 0
    # Dry-run fungerar ändå, utan nyckel och utan nätverk.
    assert service.run(prompts.EXTRACTION_TASK, limit=5, dry_run=True)["jobs"] == 1


def test_reviewer_timeouts_must_stay_within_agents_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("REVIEWER_HARD_TIMEOUT_SECONDS", "240")
    with pytest.raises(ValueError, match="REVIEWER"):
        Settings.from_env()
    monkeypatch.setenv("REVIEWER_HARD_TIMEOUT_SECONDS", "120")
    settings = Settings.from_env()
    assert settings.reviewer_enabled and settings.reviewer_model == "claude-haiku-5-5"


def test_dry_run_makes_no_api_call(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    result = service.run(prompts.EXTRACTION_TASK, limit=5, dry_run=True)
    assert result["status"] == "dry_run"
    assert result["jobs"] == 1
    assert result["estimated_input_tokens"] > 500
    assert 0 < result["estimated_cost_usd"] < 0.01
    assert "12 345 TSEK" in result["prepared"][0].user
    assert client.messages.calls == []
    assert _count(db, "review_runs") == 0


def test_hard_timeout_is_recorded_and_retry_is_explicit(tmp_path: Path) -> None:
    settings_kw = {"reviewer_hard_timeout_seconds": 0.05}
    service, db, ids, client = _service(tmp_path, [], **settings_kw)
    client.messages.delay = 1.0
    client.messages.responses.append(_message(_good_review(ids)))
    assert service.run(prompts.EXTRACTION_TASK, limit=5)["counts"] == {"timeout": 1}
    # Utan --retry-failed görs inget nytt försök.
    assert service.prepare_extraction_reviews(limit=5) == []

    client.messages.delay = 0.0
    client.messages.responses.append(_message(_good_review(ids)))
    retry = ReviewService(db, service.settings, provider=AnthropicReviewProvider(
        service.settings, client=client), clock=lambda: NOW)
    result = retry.run(prompts.EXTRACTION_TASK, limit=5, retry_failed=True)
    assert result["counts"] == {"completed": 1}
    with db.connect() as c:
        run = c.execute("SELECT status, attempt_count FROM review_runs").fetchone()
    assert (run["status"], run["attempt_count"]) == ("completed", 2)


def _seed_filtered_headlines(db: Database, issuer: int) -> list[int]:
    titles = [
        ("Testbolaget säkrar finansiering om 15 MSEK", 0, "ingen kassasignal i metadata"),
        ("Testbolaget utser ny CFO", 10, "personal"),
        ("Inbjudan till presentation av Q3", 0, "kalender/inbjudan utan ny kassahändelse"),
    ]
    ids = []
    with db.connect() as c:
        for index, (title, score, reason) in enumerate(titles):
            ids.append(c.execute(
                """INSERT INTO documents (issuer_id, event_key, canonical_url, title,
                   published_at, cash_relevance_score, relevance_reason,
                   queued_for_analysis, created_at, updated_at)
                   VALUES (?, ?, 'https://mfn.se/x', ?, ?, ?, ?, 0, ?, ?)""",
                (issuer, f"h{index}", title, NOW, score, reason, NOW, NOW),
            ).lastrowid)
    return ids


def test_headline_batch_review_flags_missed_documents(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [])
    doc_ids = _seed_filtered_headlines(db, ids["issuer"])
    client.messages.responses.append(_message({"documents": [
        {"document_id": doc_ids[0], "may_affect_cash": True,
         "reasoning": "Säkrad finansiering påverkar kassan."},
        {"document_id": doc_ids[1], "may_affect_cash": False, "reasoning": "Personalnyhet."},
        {"document_id": doc_ids[2], "may_affect_cash": False, "reasoning": "Kalender."},
    ]}))
    jobs_before = _count(db, "extraction_jobs")
    before = _snapshot(db)

    result = service.run(prompts.HEADLINE_TASK, limit=50)
    assert result["counts"] == {"completed": 1}
    assert len(client.messages.calls) == 1
    user = client.messages.calls[0]["messages"][0]["content"]
    assert "säkrar finansiering" in user
    assert "Delårsrapport" not in user  # släpptes vidare av filtret, granskas inte här

    with db.connect() as c:
        rows = {
            r["target_id"]: (r["verdict"], r["source_quote"])
            for r in c.execute(
                "SELECT target_id, verdict, source_quote FROM review_verdicts "
                "WHERE task=? AND target_kind='document'", (prompts.HEADLINE_TASK,),
            )
        }
    assert rows[doc_ids[0]] == ("flag", "Testbolaget säkrar finansiering om 15 MSEK")
    assert rows[doc_ids[1]][0] == "no_flag"
    # Granskaren startar ingen extraction och rör inte dokumenten.
    assert _count(db, "extraction_jobs") == jobs_before
    assert _snapshot(db) == before
    # Redan granskade rubriker plockas inte upp igen.
    assert service.prepare_headline_reviews(limit=50) == []


def test_headline_batches_respect_batch_size(tmp_path: Path) -> None:
    service, db, ids, client = _service(tmp_path, [], reviewer_headline_batch_size=2)
    _seed_filtered_headlines(db, ids["issuer"])
    prepared = service.prepare_headline_reviews(limit=50)
    assert [len(p.targets) for p in prepared] == [2, 1]


def test_cli_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from capital_radar.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SCREENER_DB_PATH", str(tmp_path / "screener.db"))
    db = Database(tmp_path / "screener.db")
    db.initialize()
    _seed(db)
    assert main(["review", "extractions", "-n", "3", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "status: dry_run" in out and "extraction_job" in out and "tokens in" in out
