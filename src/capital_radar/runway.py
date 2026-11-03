from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .analysis_store import AnalysisStore
from .lcs_fit import compute_bergs_fit
from .normalization import stable_hash, utc_now


def compute_runways(store: AnalysisStore) -> dict[str, Any]:
    as_of_date = datetime.now(timezone.utc).date().isoformat()
    report_inputs = _latest_report_inputs(store)
    output: dict[str, Any] = {
        "eligible_reports": len(report_inputs),
        "created": 0,
        "unchanged": 0,
        "interpretations_created": 0,
        "interpretations_unchanged": 0,
        "skipped": [],
        "assessments": [],
    }
    for report in report_inputs:
        missing = [
            key
            for key in ("cash", "operating_cash_flow", "period_months", "currency")
            if report.get(key) is None
        ]
        if missing:
            output["skipped"].append(
                {
                    "issuer": report["issuer_name"],
                    "reason": f"Saknar {', '.join(missing)}",
                }
            )
            continue
        try:
            assessment = _calculate_assessment(
                store, report=report, as_of_date=as_of_date
            )
        except ValueError as exc:
            # Failure isolation: en issuer med ogiltig rapportperiod (t.ex.
            # förlängt räkenskapsår, 18 mån) får inte krascha hela batchen.
            output["skipped"].append(
                {"issuer": report["issuer_name"], "reason": str(exc)}
            )
            continue
        created = _save_assessment(store, assessment)
        output["created" if created else "unchanged"] += 1
        interpretation_created = _save_interpretation(store, assessment)
        output[
            "interpretations_created"
            if interpretation_created
            else "interpretations_unchanged"
        ] += 1
        output["assessments"].append(
            {
                "issuer": report["issuer_name"],
                "operating_runway_months": assessment["operating_runway_months"],
                "adjusted_runway_months": assessment["adjusted_runway_months"],
                "operating_need_score": assessment["operating_need_score"],
                "financing_adjustment_points": assessment[
                    "financing_adjustment_points"
                ],
                "adjusted_need_score": assessment["adjusted_need_score"],
                "need_timing": assessment["need_timing"],
                "alert_level": assessment["alert_level"],
                "evidence_posture": assessment["evidence_posture"],
                "bergs_fit": assessment.get("bergs_fit"),
            }
        )
    output["assessments"].sort(
        key=lambda item: (-item["adjusted_need_score"], item["issuer"])
    )
    return output


def _latest_report_inputs(store: AnalysisStore) -> list[dict[str, Any]]:
    with store.db.connect() as connection:
        reports = connection.execute(
            """
            SELECT r.issuer_id, i.canonical_name, r.document_id,
                   r.report_period_end, r.currency, r.period_months,
                   r.confidence, r.requires_human_review,
                   j.parsed_output_json
            FROM report_extractions r
            JOIN issuers i ON i.id=r.issuer_id
            JOIN extraction_jobs j ON j.id=r.extraction_job_id
            WHERE r.is_financial_report=1
              AND r.report_period_end IS NOT NULL
              AND r.id=(
                  SELECT COALESCE(
                      (
                          SELECT r2.id FROM report_extractions r2
                          WHERE r2.issuer_id=r.issuer_id
                            AND r2.is_financial_report=1
                            AND r2.report_period_end IS NOT NULL
                            AND EXISTS (
                                SELECT 1 FROM financial_facts fc
                                WHERE fc.document_id=r2.document_id
                                  AND fc.fact_type='cash_and_cash_equivalents'
                                  AND fc.normalized_value IS NOT NULL
                            )
                            AND EXISTS (
                                SELECT 1 FROM financial_facts fo
                                WHERE fo.document_id=r2.document_id
                                  AND fo.fact_type='operating_cash_flow'
                                  AND fo.normalized_value IS NOT NULL
                            )
                          ORDER BY r2.report_period_end DESC, r2.id DESC
                          LIMIT 1
                      ),
                      (
                          SELECT r3.id FROM report_extractions r3
                          WHERE r3.issuer_id=r.issuer_id
                            AND r3.is_financial_report=1
                            AND r3.report_period_end IS NOT NULL
                          ORDER BY r3.report_period_end DESC, r3.id DESC
                          LIMIT 1
                      )
                  )
              )
            ORDER BY i.canonical_name
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        for report in reports:
            facts = connection.execute(
                """
                SELECT fact_type, normalized_value, currency, confidence,
                       source_page, source_excerpt
                FROM financial_facts
                WHERE document_id=?
                  AND fact_type IN (
                      'cash_and_cash_equivalents',
                      'operating_cash_flow',
                      'debt_due_within_12_months'
                  )
                  AND normalized_value IS NOT NULL
                ORDER BY confidence DESC, id DESC
                """,
                (report["document_id"],),
            ).fetchall()
            by_type: dict[str, Any] = {}
            for fact in facts:
                by_type.setdefault(str(fact["fact_type"]), fact)
            cash = by_type.get("cash_and_cash_equivalents")
            cfo = by_type.get("operating_cash_flow")
            debt_due = by_type.get("debt_due_within_12_months")
            bergs_fit = _bergs_fit_from_parsed(report["parsed_output_json"])
            output.append(
                {
                    "issuer_id": int(report["issuer_id"]),
                    "issuer_name": str(report["canonical_name"]),
                    "report_document_id": int(report["document_id"]),
                    "report_period_end": str(report["report_period_end"]),
                    "currency": report["currency"]
                    or (cash["currency"] if cash else None),
                    "period_months": report["period_months"],
                    "cash": cash["normalized_value"] if cash else None,
                    "operating_cash_flow": cfo["normalized_value"] if cfo else None,
                    "debt_due_within_12_months": (
                        debt_due["normalized_value"] if debt_due else None
                    ),
                    "report_confidence": float(report["confidence"]),
                    "requires_human_review": bool(
                        report["requires_human_review"]
                    ),
                    "cash_evidence": dict(cash) if cash else None,
                    "cfo_evidence": dict(cfo) if cfo else None,
                    "bergs_fit": bergs_fit,
                }
            )
        return output


def _calculate_assessment(
    store: AnalysisStore,
    *,
    report: dict[str, Any],
    as_of_date: str,
) -> dict[str, Any]:
    events = _post_report_events(
        store,
        issuer_id=report["issuer_id"],
        report_period_end=report["report_period_end"],
        currency=report["currency"],
        as_of_date=as_of_date,
    )
    # Deduplicera events: samma typ + belopp + beskrivning = samma h�ndelse
    seen_keys: set[tuple] = set()
    unique_events: list[dict[str, Any]] = []
    for event in events:
        key = (
            event["event_type"],
            event.get("cash_effect_amount"),
            event.get("committed_amount"),
            event.get("conditional_amount"),
            (event.get("description") or "")[:100],  # F�rsta 100 tecken r�cker
        )
        if key not in seen_keys:
            seen_keys.add(key)
            unique_events.append(event)
    confirmed_adjustment = 0.0
    committed = 0.0
    conditional = 0.0
    included_event_ids: list[int] = []

    def _normalize_amount(value: float, currency: str | None) -> float:
        """Konvertera orimligt små belopp till kronor.
        
        Heuristik:
        - < 1000: troligen MSEK (miljoner) → multiplicera med 1 000 000
        - 1000-500000: troligen kSEK (tusentals) → multiplicera med 1 000
        - >= 500000: troligen redan SEK → behåll
        """
        amount = abs(float(value))
        if currency != "SEK":
            return amount
        if amount < 1000:
            return amount * 1_000_000  # MSEK → SEK
        if amount < 500000:
            return amount * 1_000  # kSEK → SEK
        return amount

    for event in unique_events:
        currency = event.get("currency")
        if event["liquidity_effect_kind"] == "confirmed_cash":
            if (
                event["status"] == "completed"
                and event["reflected_in_report"] in ("no", "unknown", None)
                and event["cash_effect_amount"] is not None
            ):
                amount = _normalize_amount(event["cash_effect_amount"], currency)
                confirmed_adjustment += (
                    amount if event["cash_direction"] == "inflow" else -amount
                )
                included_event_ids.append(int(event["id"]))
        elif (
            event["liquidity_effect_kind"] == "committed_liquidity"
            and event["committed_amount"] is not None
            and event["status"] not in {"cancelled", "unknown"}
        ):
            committed += _normalize_amount(event["committed_amount"], currency)
            included_event_ids.append(int(event["id"]))
        elif (
            event["liquidity_effect_kind"] == "conditional_liquidity"
            and event["conditional_amount"] is not None
            and event["status"] != "cancelled"
        ):
            conditional += _normalize_amount(event["conditional_amount"], currency)
            included_event_ids.append(int(event["id"]))

    cash = float(report["cash"])
    cfo = float(report["operating_cash_flow"])
    period_months = float(report["period_months"])
    if period_months <= 0 or period_months > 12:
        raise ValueError(
            f"Ogiltig rapportperiod för {report['issuer_name']}: {period_months}"
        )
    monthly_burn = max(0.0, -cfo / period_months)
    adjusted_cash = cash + confirmed_adjustment
    weighted_liquidity = adjusted_cash + 0.70 * committed
    operating_runway = _runway(cash, monthly_burn)
    adjusted_runway = _runway(weighted_liquidity, monthly_burn)
    operating_score = _need_score(operating_runway, monthly_burn)
    adjusted_score = _need_score(adjusted_runway, monthly_burn)
    posture = (
        "preliminary_human_review_required"
        if report["requires_human_review"] or report["report_confidence"] < 0.85
        else "preliminary_automated_screen"
    )
    calculation = {
        "version": "runway-v2",
        "cash_definition": "cash_and_cash_equivalents_only",
        "burn_definition": "max(0,-operating_cash_flow/period_months)",
        "committed_liquidity_weight": 0.70,
        "conditional_liquidity_weight": 0.0,
        "debt_due_within_12_months": report["debt_due_within_12_months"],
        "included_event_ids": included_event_ids,
        "warnings": [
            "Preliminary screen; verify primary-source excerpts before outreach.",
            "Debt maturities and milestone-driven spend are not subtracted from runway.",
            "Only issuer-level financing strictly after the report period can adjust reported cash.",
        ],
    }
    # Varna om events exkluderades trots att de verkade relevanta
    excluded_events = [
        e for e in unique_events if int(e["id"]) not in included_event_ids
    ]
    if excluded_events:
        calculation["warnings"].append(
            f"VARNING: {len(excluded_events)} finansieringshändelser exkluderades: "
            + "; ".join(
                f"id={e['id']} status={e.get('status')} "
                f"kind={e.get('liquidity_effect_kind')} "
                f"reflected={e.get('reflected_in_report')}"
                for e in excluded_events[:5]
            )
        )

    assessment: dict[str, Any] = {
        "issuer_id": report["issuer_id"],
        "as_of_date": as_of_date,
        "report_document_id": report["report_document_id"],
        "report_period_end": report["report_period_end"],
        "currency": report["currency"],
        "baseline_cash": cash,
        "operating_cash_flow": cfo,
        "period_months": period_months,
        "monthly_operating_burn": monthly_burn or None,
        "confirmed_post_report_adjustment": confirmed_adjustment,
        "committed_liquidity": committed,
        "conditional_liquidity": conditional,
        "adjusted_cash": adjusted_cash,
        "weighted_liquidity": weighted_liquidity,
        "operating_runway_months": operating_runway,
        "adjusted_runway_months": adjusted_runway,
        "operating_need_score": operating_score,
        "financing_adjustment_points": adjusted_score - operating_score,
        "adjusted_need_score": adjusted_score,
        "need_timing": _timing(adjusted_runway, monthly_burn),
        "alert_level": _alert(adjusted_score),
        "evidence_posture": posture,
        "calculation_json": json.dumps(
            calculation, ensure_ascii=False, sort_keys=True
        ),
    }
    assessment["calculation_sha256"] = stable_hash(
        str(report["issuer_id"]),
        as_of_date,
        str(report["report_document_id"]),
        json.dumps(assessment, ensure_ascii=False, sort_keys=True),
    )
    assessment["bergs_fit"] = report.get("bergs_fit")
    return assessment


def _post_report_events(
    store: AnalysisStore,
    *,
    issuer_id: int,
    report_period_end: str,
    currency: str,
    as_of_date: str,
) -> list[dict[str, Any]]:
    with store.db.connect() as connection:
        rows = connection.execute(
            """
            SELECT fe.*
            FROM financing_events fe
            WHERE fe.issuer_id=?
              AND (currency=? OR currency IS NULL)
              AND (
                  -- Kassa vid periodslut innehåller redan händelser samma dag.
                  -- Endast händelser efter rapportperioden får justera kassan.
                  (COALESCE(effective_date, announcement_date) IS NOT NULL
                   AND COALESCE(effective_date, announcement_date)>?
                   AND COALESCE(effective_date, announcement_date)<=?)
                  OR
                  -- Events utan datum: inkludera om status är completed
                  -- (antag att de är post-report tills motsatsen är bevisad)
                  (COALESCE(effective_date, announcement_date) IS NULL
                   AND status='completed')
              )
              -- Portföljbolags finansiering är inte likviditet hos emittenten.
              AND NOT EXISTS (
                  SELECT 1
                  FROM financing_event_evidence fee
                  JOIN documents evidence_document
                    ON evidence_document.id=fee.document_id
                  WHERE fee.financing_event_id=fe.id
                    AND (
                      LOWER(evidence_document.title) LIKE '%portföljbolag%'
                      OR LOWER(evidence_document.title) LIKE '%portfolio company%'
                    )
              )
            ORDER BY COALESCE(effective_date, announcement_date), fe.id
            """,
            (issuer_id, currency, report_period_end, as_of_date),
        ).fetchall()
    return [dict(row) for row in rows]


def _runway(liquidity: float, monthly_burn: float) -> float | None:
    if monthly_burn <= 0:
        return None
    return round(max(0.0, liquidity) / monthly_burn, 1)


def _need_score(runway: float | None, monthly_burn: float) -> int:
    if monthly_burn <= 0 or runway is None:
        return 0
    if runway < 3:
        return 100
    if runway < 6:
        return 85
    if runway < 9:
        return 70
    if runway < 12:
        return 55
    if runway < 18:
        return 35
    if runway < 24:
        return 20
    return 5


def _timing(runway: float | None, monthly_burn: float) -> str:
    if monthly_burn <= 0 or runway is None:
        return "no_negative_operating_cash_flow_signal"
    if runway < 3:
        return "0-3_months"
    if runway < 6:
        return "3-6_months"
    if runway < 12:
        return "6-12_months"
    if runway < 18:
        return "12-18_months"
    if runway < 24:
        return "18-24_months"
    return "more_than_24_months"


def _alert(score: int) -> str:
    if score >= 85:
        return "critical"
    if score >= 70:
        return "high"
    if score >= 55:
        return "medium"
    if score >= 35:
        return "watch"
    return "low"


def _save_assessment(store: AnalysisStore, assessment: dict[str, Any]) -> bool:
    with store.db.connect() as connection:
        # Assessment IDs are historical references used by saved analyses and
        # agent application readbacks. A new calculation must retain earlier
        # reported facts and their foreign keys; latest-row readers already
        # select a single assessment explicitly.
        # Försök uppdatera befintlig assessment med samma hash
        cursor = connection.execute(
            """
            UPDATE runway_assessments SET
                as_of_date = :as_of_date,
                report_document_id = :report_document_id,
                report_period_end = :report_period_end,
                currency = :currency,
                baseline_cash = :baseline_cash,
                operating_cash_flow = :operating_cash_flow,
                period_months = :period_months,
                monthly_operating_burn = :monthly_operating_burn,
                confirmed_post_report_adjustment = :confirmed_post_report_adjustment,
                committed_liquidity = :committed_liquidity,
                conditional_liquidity = :conditional_liquidity,
                adjusted_cash = :adjusted_cash,
                weighted_liquidity = :weighted_liquidity,
                operating_runway_months = :operating_runway_months,
                adjusted_runway_months = :adjusted_runway_months,
                operating_need_score = :operating_need_score,
                financing_adjustment_points = :financing_adjustment_points,
                adjusted_need_score = :adjusted_need_score,
                need_timing = :need_timing,
                alert_level = :alert_level,
                evidence_posture = :evidence_posture,
                calculation_json = :calculation_json
            WHERE calculation_sha256 = :calculation_sha256
            """,
            assessment,
        )
        if cursor.rowcount == 1:
            return False  # Uppdaterad, inte skapad
        
        # Ingen befintlig rad — skapa ny
        cursor = connection.execute(
            """
            INSERT INTO runway_assessments (
                issuer_id, as_of_date, report_document_id, report_period_end,
                currency, baseline_cash, operating_cash_flow, period_months,
                monthly_operating_burn, confirmed_post_report_adjustment,
                committed_liquidity, conditional_liquidity, adjusted_cash,
                weighted_liquidity, operating_runway_months,
                adjusted_runway_months, operating_need_score,
                financing_adjustment_points, adjusted_need_score, need_timing,
                alert_level, evidence_posture, calculation_json,
                calculation_sha256, created_at
            ) VALUES (
                :issuer_id, :as_of_date, :report_document_id, :report_period_end,
                :currency, :baseline_cash, :operating_cash_flow, :period_months,
                :monthly_operating_burn, :confirmed_post_report_adjustment,
                :committed_liquidity, :conditional_liquidity, :adjusted_cash,
                :weighted_liquidity, :operating_runway_months,
                :adjusted_runway_months, :operating_need_score,
                :financing_adjustment_points, :adjusted_need_score, :need_timing,
                :alert_level, :evidence_posture, :calculation_json,
                :calculation_sha256, :created_at
            )
            """,
            {**assessment, "created_at": utc_now()},
        )
        return cursor.rowcount == 1


def _save_interpretation(
    store: AnalysisStore, assessment: dict[str, Any]
) -> bool:
    report_document_id = int(assessment["report_document_id"])
    calculation = json.loads(assessment["calculation_json"])
    event_ids = [int(value) for value in calculation["included_event_ids"]]
    with store.db.connect() as connection:
        issuer = connection.execute(
            "SELECT canonical_name FROM issuers WHERE id=?",
            (assessment["issuer_id"],),
        ).fetchone()
        job = connection.execute(
            """
            SELECT model, parsed_output_json
            FROM extraction_jobs
            WHERE document_id=? AND status='completed'
            ORDER BY id DESC LIMIT 1
            """,
            (report_document_id,),
        ).fetchone()
        facts = connection.execute(
            """
            SELECT fact_type, normalized_value, source_page, source_excerpt
            FROM financial_facts
            WHERE document_id=?
              AND fact_type IN (
                  'cash_and_cash_equivalents', 'operating_cash_flow'
              )
            ORDER BY confidence DESC, id DESC
            """,
            (report_document_id,),
        ).fetchall()
        report = connection.execute(
            """
            SELECT confidence
            FROM report_extractions
            WHERE document_id=? ORDER BY id DESC LIMIT 1
            """,
            (report_document_id,),
        ).fetchone()
        events: list[Any] = []
        event_evidence: list[Any] = []
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            events = connection.execute(
                f"""
                SELECT id, event_type, description
                FROM financing_events
                WHERE id IN ({placeholders})
                ORDER BY COALESCE(effective_date, announcement_date) DESC, id
                """,
                event_ids,
            ).fetchall()
            event_evidence = connection.execute(
                f"""
                SELECT e.financing_event_id, e.document_id, e.source_page,
                       e.source_excerpt, e.evidence_label
                FROM financing_event_evidence e
                WHERE e.financing_event_id IN ({placeholders})
                ORDER BY e.financing_event_id, e.id
                """,
                event_ids,
            ).fetchall()

    parsed_output: dict[str, Any] = {}
    if job and job["parsed_output_json"]:
        try:
            parsed_output = json.loads(str(job["parsed_output_json"]))
        except json.JSONDecodeError:
            parsed_output = {}
    document_summary = str(parsed_output.get("document_summary") or "").strip()
    issuer_name = str(issuer["canonical_name"]) if issuer else "Bolaget"
    operating = assessment["operating_runway_months"]
    adjusted = assessment["adjusted_runway_months"]
    headline = _interpretation_headline(operating, adjusted)
    capital_need = _capital_need_text(assessment)
    recommended_action = _recommended_action(assessment)
    bergs_fit = assessment.get("bergs_fit")
    if not isinstance(bergs_fit, dict):
        bergs_fit = {}
    summary = (
        f"{issuer_name} har en beräknad operativ runway på "
        f"{_months_text(operating)} och en finansieringsjusterad runway på "
        f"{_months_text(adjusted)}. "
        f"{document_summary}"
    ).strip()
    transaction_context = "; ".join(
        f"{row['event_type']}: {row['description']}" for row in events[:3]
    ) or None
    evidence = [
        {
            "document_id": report_document_id,
            "page": row["source_page"],
            "label": row["fact_type"],
            "excerpt": row["source_excerpt"],
        }
        for row in facts
        if row["source_excerpt"]
    ]
    evidence.extend(
        {
            "document_id": row["document_id"],
            "page": row["source_page"],
            "label": row["evidence_label"],
            "excerpt": row["source_excerpt"],
        }
        for row in event_evidence
        if row["source_excerpt"]
    )
    source_document_ids = sorted(
        {
            report_document_id,
            *(
                int(row["document_id"])
                for row in event_evidence
                if row["document_id"] is not None
            ),
        }
    )
    item = {
        "issuer_id": assessment["issuer_id"],
        "as_of_date": assessment["as_of_date"],
        "method": (
            f"{job['model'] if job else 'Kimi'}-extraktion + runway-v1"
        ),
        "need_level": assessment["alert_level"],
        "need_score": assessment["adjusted_need_score"],
        "need_timing": assessment["need_timing"],
        "headline": headline,
        "summary": summary,
        "capital_need": capital_need,
        "transaction_context": transaction_context,
        "recommended_action": recommended_action,
        "bergs_fit_score": bergs_fit.get("score"),
        "bergs_fit_summary": bergs_fit.get("rationale"),
        "cash_amount": assessment["baseline_cash"],
        "operating_cash_flow": assessment["operating_cash_flow"],
        "period_months": assessment["period_months"],
        "currency": assessment["currency"],
        "report_period_end": assessment["report_period_end"],
        "confidence": float(report["confidence"]) if report else 0.7,
        "evidence_posture": assessment["evidence_posture"],
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
        "source_document_ids_json": json.dumps(source_document_ids),
        "interpretation_sha256": stable_hash(
            str(assessment["issuer_id"]),
            assessment["calculation_sha256"],
            document_summary,
            json.dumps(event_ids),
            headline,
            capital_need,
            recommended_action,
            str(bergs_fit.get("score")),
            str(bergs_fit.get("mandate_type")),
        ),
    }
    return store.save_interpretation(item)


def _bergs_fit_from_parsed(value: Any) -> dict[str, Any]:
    fallback = {
        "score": None,
        "level": "not_assessable",
        "mandate_type": "not_assessable",
        "rationale": "Bergs-fit saknas i det cachelagrade Kimi-svaret.",
        "next_step": "Kör ingen ny AI-analys enbart för Bergs-fit.",
        "confidence": 0.0,
    }
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    fit = parsed.get("bergs_fit")
    if isinstance(fit, dict):
        # Legacy-extraktion med inbäddat bergs_fit — återanvänd orört.
        return dict(fit)
    # V2-extraktion saknar bergs_fit — beräkna deterministiskt downstream.
    return compute_bergs_fit(parsed)


def _interpretation_headline(
    operating_runway: float | None, adjusted_runway: float | None
) -> str:
    if operating_runway is None:
        return "Ingen negativ operativ kassaflödessignal i senaste rapporten"
    if adjusted_runway is not None and adjusted_runway > operating_runway:
        return (
            f"Operativ runway {_months_text(operating_runway)}; "
            f"{_months_text(adjusted_runway)} inklusive viktad finansiering"
        )
    return f"Beräknad runway {_months_text(operating_runway)}"


def _capital_need_text(assessment: dict[str, Any]) -> str:
    operating = assessment["operating_runway_months"]
    adjusted = assessment["adjusted_runway_months"]
    timing = _timing_text(str(assessment["need_timing"]))
    if operating is None:
        return (
            "Senaste rapporten visar inget negativt operativt kassaflöde som "
            "ger en meningsfull runway-beräkning."
        )
    if adjusted is not None and adjusted - operating >= 3:
        return (
            f"Utan identifierad extern finansiering är runway endast "
            f"{_months_text(operating)}. Med viktad, ännu verifieringskrävande "
            f"finansiering bedöms kapitalbehovet uppstå {timing}."
        )
    return (
        f"Kapitalbehovet bedöms till {timing} utifrån en runway på "
        f"{_months_text(adjusted)}."
    )


def _recommended_action(assessment: dict[str, Any]) -> str:
    score = int(assessment["adjusted_need_score"])
    if score >= 70:
        return (
            "Prioritera bolaget för manuell verifiering och möjlig kontakt. "
            "Bekräfta kassa, burn rate och finansieringens genomförandestatus."
        )
    if score >= 35:
        return (
            "Bevaka genomförandet av annonserad finansiering och verifiera "
            "villkor, nettolikvid och tidplan före eventuell kontakt."
        )
    return (
        "Låg omedelbar prioritet. Uppdatera bedömningen vid nästa rapport "
        "eller ny kassapåverkande finansieringshändelse."
    )


def _months_text(value: float | None) -> str:
    if value is None:
        return "ej beräkningsbar"
    return f"{value:.1f}".replace(".", ",") + " månader"


def _timing_text(value: str) -> str:
    labels = {
        "0-3_months": "inom 0–3 månader",
        "3-6_months": "inom 3–6 månader",
        "6-12_months": "inom 6–12 månader",
        "12-18_months": "inom 12–18 månader",
        "18-24_months": "inom 18–24 månader",
        "more_than_24_months": "senare än 24 månader",
        "no_negative_operating_cash_flow_signal": "utan tydlig tidpunkt",
    }
    return labels.get(value, value)
