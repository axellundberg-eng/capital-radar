from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .db import Database
from .document_quality import evaluate_document_quality
from .financing_history import FinancingHistoryStore, GuaranteeTranche
from .normalization import stable_hash, utc_now


@dataclass(frozen=True)
class AttachmentTarget:
    attachment_id: int | None
    url: str
    mime_type: str | None
    content_status: str | None


@dataclass(frozen=True)
class FetchCandidate:
    document_id: int
    issuer_id: int
    issuer_name: str
    title: str
    published_at: str
    profile_category: str
    language: str | None
    urls: tuple[AttachmentTarget, ...]


@dataclass(frozen=True)
class AnalysisCandidate:
    document_id: int
    issuer_id: int
    issuer_name: str
    title: str
    published_at: str
    profile_category: str
    language: str | None
    content_id: int
    source_url: str
    extracted_text: str
    text_sha256: str
    page_count: int | None


def _url_rank(url: str) -> int:
    host = (urlsplit(url).hostname or "").casefold()
    if host == "attachment.news.eu.nasdaq.com":
        return 0
    if host in {"storage.mfn.se", "mb.cision.com", "cdn.bequoted.com"}:
        return 1
    if host in {"view.news.eu.nasdaq.com", "www.mfn.se", "mfn.se"}:
        return 2
    return 9


class AnalysisStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def fetch_candidates(
        self,
        limit: int,
        issuer_id: int | None = None,
        prefer_reports: bool = False,
        document_id: int | None = None,
    ) -> list[FetchCandidate]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id AS document_id, d.issuer_id, i.canonical_name,
                       d.title, d.published_at, d.profile_category, d.language,
                       a.id AS attachment_id, a.url, a.mime_type,
                       ac.status AS attachment_status
                FROM documents d
                JOIN issuers i ON i.id=d.issuer_id
                LEFT JOIN attachments a ON a.document_id=d.id
                LEFT JOIN document_contents ac
                  ON ac.document_id=d.id AND ac.source_url=a.url
                WHERE (d.queued_for_analysis=1 OR ? IS NOT NULL)
                  AND (? IS NULL OR d.id=?)
                  AND (? IS NULL OR d.issuer_id=?)
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM document_contents c
                          WHERE c.document_id=d.id
                            AND c.status IN (
                                'extracted', 'needs_ocr', 'too_short'
                            )
                            AND c.extracted_text IS NOT NULL
                      )
                      OR (
                          d.profile_category='report'
                          AND NOT EXISTS (
                              SELECT 1 FROM document_contents qc
                              WHERE qc.document_id=d.id
                                AND qc.status='extracted'
                                AND qc.quality_status='complete'
                          )
                          AND EXISTS (
                              SELECT 1
                              FROM attachments a2
                              LEFT JOIN document_contents c2
                                ON c2.document_id=d.id
                               AND c2.source_url=a2.url
                              WHERE a2.document_id=d.id
                                AND (
                                    c2.id IS NULL OR c2.status='failed'
                                )
                          )
                      )
                  )
                ORDER BY CASE
                             WHEN ?=1 AND d.profile_category='report' THEN 0
                             ELSE 1
                         END,
                         d.cash_relevance_score DESC, d.published_at DESC,
                         d.id ASC, a.id
                """,
                (document_id, document_id, document_id, issuer_id, issuer_id, int(prefer_reports)),
            ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            document_id = int(row["document_id"])
            item = grouped.setdefault(
                document_id,
                {
                    "document_id": document_id,
                    "issuer_id": int(row["issuer_id"]),
                    "issuer_name": row["canonical_name"],
                    "title": row["title"],
                    "published_at": row["published_at"],
                    "profile_category": row["profile_category"],
                    "language": row["language"],
                    "urls": [],
                },
            )
            if row["url"]:
                item["urls"].append(
                    AttachmentTarget(
                        attachment_id=row["attachment_id"],
                        url=str(row["url"]),
                        mime_type=row["mime_type"],
                        content_status=row["attachment_status"],
                    )
                )
        candidates: list[FetchCandidate] = []
        for item in grouped.values():
            canonical = self._canonical_url(item["document_id"])
            if canonical and not any(target.url == canonical for target in item["urls"]):
                item["urls"].append(
                    AttachmentTarget(
                        attachment_id=None,
                        url=canonical,
                        mime_type=None,
                        content_status=None,
                    )
                )
            item["urls"].sort(
                key=lambda entry: (_url_rank(entry.url), entry.url)
            )
            candidates.append(FetchCandidate(**item))
            if len(candidates) >= limit:
                break
        return candidates

    def _canonical_url(self, document_id: int) -> str:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT canonical_url FROM documents WHERE id=?", (document_id,)
            ).fetchone()
        if not row:
            raise KeyError(document_id)
        return str(row["canonical_url"])

    def document_readiness(self, document_id: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            document = connection.execute(
                """
                SELECT id, issuer_id, title, published_at, profile_category
                FROM documents WHERE id=?
                """,
                (document_id,),
            ).fetchone()
            if not document:
                raise KeyError(document_id)
            content = connection.execute(
                """
                SELECT id AS content_id, source_url, status,
                       quality_status, quality_score,
                       quality_reason, page_count, char_count
                FROM document_contents
                WHERE document_id=?
                ORDER BY
                    CASE
                        WHEN quality_status='complete' THEN 0
                        WHEN quality_status='not_required' THEN 1
                        WHEN status='extracted' THEN 2
                        WHEN status='needs_ocr' THEN 3
                        ELSE 4
                    END,
                    quality_score DESC, char_count DESC, id
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        payload = dict(document)
        if not content:
            payload.update(
                {
                    "readiness": "unavailable",
                    "reason": "Ingen bilaga har kunnat läsas",
                }
            )
            return payload
        payload.update(dict(content))
        if content["quality_status"] == "complete":
            payload["readiness"] = "complete"
            payload["reason"] = content["quality_reason"]
        elif (
            document["profile_category"] != "report"
            and content["status"] == "extracted"
        ):
            payload["readiness"] = "complete"
            payload["reason"] = "Dokumenttext tillgänglig"
        elif content["status"] == "needs_ocr":
            payload["readiness"] = "needs_ocr"
            payload["reason"] = "Bilagan behöver OCR"
        elif content["status"] == "extracted":
            payload["readiness"] = "insufficient_facts"
            payload["reason"] = (
                content["quality_reason"]
                or "Rapporten saknar nödvändiga runway-fakta"
            )
        else:
            payload["readiness"] = "unavailable"
            payload["reason"] = "Bilagan kunde inte hämtas eller läsas"
        return payload

    def latest_report_readiness(self, issuer_id: int) -> dict[str, Any] | None:
        """Readiness for bolagets rapport som ska analyseras.

        Valjer den senaste rapporten vars readiness ar ``complete``. En nyare
        men ofullstandig rapport (t.ex. en bilaga som inte kvalitetsgodkants)
        far inte blockera analysen nar en nagot aldre men komplett rapport
        finns. Om ingen rapport ar complete returneras den senaste rapportens
        readiness oforandrad, sa att anroparen kan rapportera orsaken.
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM documents
                WHERE issuer_id=? AND profile_category='report'
                  AND queued_for_analysis=1
                ORDER BY published_at DESC, id ASC
                LIMIT 10
                """,
                (issuer_id,),
            ).fetchall()
        if not rows:
            return None
        fallback: dict[str, Any] | None = None
        for row in rows:
            readiness = self.document_readiness(int(row["id"]))
            if fallback is None:
                fallback = readiness
            if readiness["readiness"] == "complete":
                return readiness
        return fallback

    def regrade_document_quality(self, document_id: int) -> int:
        now = utc_now()
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.extracted_text, c.page_count,
                       d.profile_category
                FROM document_contents c
                JOIN documents d ON d.id=c.document_id
                WHERE c.document_id=? AND c.status='extracted'
                  AND c.extracted_text IS NOT NULL
                """,
                (document_id,),
            ).fetchall()
            for row in rows:
                quality = evaluate_document_quality(
                    str(row["extracted_text"]),
                    profile_category=str(row["profile_category"]),
                    page_count=row["page_count"],
                )
                connection.execute(
                    """
                    UPDATE document_contents
                    SET quality_score=?, quality_status=?,
                        quality_reason=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        quality.score,
                        quality.status,
                        quality.reason,
                        now,
                        row["id"],
                    ),
                )
        return len(rows)

    def regrade_all_report_quality(self) -> dict[str, int]:
        """Kor om quality-grading for alla extraherade rapport-dokument.

        Anvands efter att signal-logiken i document_quality andrats, sa att
        befintliga dokument far den nya bedomningen utan att hamtas om.
        Returnerar antal dokument per resulterande status.
        """
        with self.db.connect() as connection:
            ids = [
                int(row["document_id"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT c.document_id
                    FROM document_contents c
                    JOIN documents d ON d.id=c.document_id
                    WHERE c.status='extracted' AND d.profile_category='report'
                      AND c.extracted_text IS NOT NULL
                    """
                ).fetchall()
            ]
        counts: dict[str, int] = {}
        for document_id in ids:
            self.regrade_document_quality(document_id)
            with self.db.connect() as connection:
                row = connection.execute(
                    """
                    SELECT quality_status FROM document_contents
                    WHERE document_id=? AND status='extracted'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (document_id,),
                ).fetchone()
            status = str(row["quality_status"]) if row else "unknown"
            counts[status] = counts.get(status, 0) + 1
        counts["documents"] = len(ids)
        return counts

    def save_content(
        self,
        *,
        candidate: FetchCandidate,
        attachment_id: int | None,
        source_url: str,
        final_url: str | None,
        local_path: str | None,
        mime_type: str | None,
        sha256: str | None,
        byte_size: int | None,
        extraction_method: str | None,
        extracted_text: str | None,
        text_sha256: str | None,
        page_count: int | None,
        status: str,
        quality_score: int = 0,
        quality_status: str = "unscored",
        quality_reason: str | None = None,
        error: str | None = None,
    ) -> int:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO document_contents (
                    document_id, attachment_id, source_url, final_url, local_path,
                    mime_type, sha256, byte_size, extraction_method,
                    extracted_text, text_sha256, char_count, page_count,
                    quality_score, quality_status, quality_reason, status,
                    error, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(document_id, source_url) DO UPDATE SET
                    attachment_id=excluded.attachment_id,
                    final_url=excluded.final_url,
                    local_path=excluded.local_path,
                    mime_type=excluded.mime_type,
                    sha256=excluded.sha256,
                    byte_size=excluded.byte_size,
                    extraction_method=excluded.extraction_method,
                    extracted_text=excluded.extracted_text,
                    text_sha256=excluded.text_sha256,
                    char_count=excluded.char_count,
                    page_count=excluded.page_count,
                    quality_score=excluded.quality_score,
                    quality_status=excluded.quality_status,
                    quality_reason=excluded.quality_reason,
                    status=excluded.status,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate.document_id,
                    attachment_id,
                    source_url,
                    final_url,
                    local_path,
                    mime_type,
                    sha256,
                    byte_size,
                    extraction_method,
                    extracted_text,
                    text_sha256,
                    len(extracted_text or ""),
                    page_count,
                    quality_score,
                    quality_status,
                    quality_reason,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM document_contents
                WHERE document_id=? AND source_url=?
                """,
                (candidate.document_id, source_url),
            ).fetchone()
            if extracted_text:
                from .evidence_store import archive_document_evidence
                archive_document_evidence(
                    connection,
                    document_id=candidate.document_id,
                    content_id=int(row["id"]),
                    text=extracted_text,
                    text_sha256=text_sha256,
                    page_count=page_count,
                )
            aggregate = connection.execute(
                """
                SELECT
                    MAX(CASE WHEN status='extracted' THEN 1 ELSE 0 END)
                        AS has_extracted,
                    MAX(CASE WHEN status='needs_ocr' THEN 1 ELSE 0 END)
                        AS has_ocr,
                    MAX(CASE WHEN status='too_short' THEN 1 ELSE 0 END)
                        AS has_short
                FROM document_contents
                WHERE document_id=?
                """,
                (candidate.document_id,),
            ).fetchone()
            if aggregate["has_extracted"]:
                analysis_status = "content_ready"
            elif aggregate["has_ocr"]:
                analysis_status = "needs_ocr"
            elif aggregate["has_short"]:
                analysis_status = "content_too_short"
            else:
                analysis_status = "content_failed"
            connection.execute(
                """
                UPDATE documents
                SET analysis_status=?, content_sha256=COALESCE(?, content_sha256),
                    updated_at=?
                WHERE id=?
                """,
                (analysis_status, sha256, now, candidate.document_id),
            )
            return int(row["id"])

    def analysis_candidates(
        self,
        limit: int,
        issuer_id: int | None = None,
        prefer_reports: bool = False,
    ) -> list[AnalysisCandidate]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id AS document_id, d.issuer_id, i.canonical_name,
                       d.title, d.published_at, d.profile_category, d.language,
                       c.id AS content_id,
                       c.source_url, c.extracted_text, c.text_sha256, c.page_count
                FROM documents d
                JOIN issuers i ON i.id=d.issuer_id
                JOIN document_contents c ON c.id=(
                    SELECT c2.id FROM document_contents c2
                    WHERE c2.document_id=d.id AND c2.status='extracted'
                      AND c2.extracted_text IS NOT NULL
                    ORDER BY
                        CASE
                            WHEN c2.quality_status='complete' THEN 0
                            WHEN c2.quality_status='not_required' THEN 1
                            ELSE 2
                        END,
                        c2.quality_score DESC,
                        CASE
                            WHEN c2.source_url LIKE
                                 'https://attachment.news.eu.nasdaq.com/%' THEN 0
                            WHEN c2.source_url LIKE
                                 'https://storage.mfn.se/%' THEN 1
                            ELSE 2
                        END,
                        c2.char_count DESC, c2.id
                    LIMIT 1
                )
                WHERE d.queued_for_analysis=1
                  AND (? IS NULL OR d.issuer_id=?)
                  AND (
                      d.profile_category!='report'
                      OR c.quality_status='complete'
                  )
                ORDER BY CASE
                             WHEN ?=1 AND d.profile_category='report' THEN 0
                             ELSE 1
                         END,
                         d.cash_relevance_score DESC, d.published_at DESC,
                         d.id ASC
                LIMIT ?
                """,
                (issuer_id, issuer_id, int(prefer_reports), limit * 3),
            ).fetchall()
        selected: list[AnalysisCandidate] = []
        seen_families: set[tuple[int, str, str]] = set()
        for row in rows:
            family = (
                int(row["issuer_id"]),
                str(row["published_at"])[:16],
                _document_family(str(row["title"])),
            )
            if family in seen_families:
                continue
            seen_families.add(family)
            selected.append(
                AnalysisCandidate(
                    document_id=int(row["document_id"]),
                    issuer_id=int(row["issuer_id"]),
                    issuer_name=str(row["canonical_name"]),
                    title=str(row["title"]),
                    published_at=str(row["published_at"]),
                    profile_category=str(row["profile_category"]),
                    language=row["language"],
                    content_id=int(row["content_id"]),
                    source_url=str(row["source_url"]),
                    extracted_text=str(row["extracted_text"]),
                    text_sha256=str(row["text_sha256"]),
                    page_count=row["page_count"],
                )
            )
            if len(selected) >= limit:
                break
        return selected

    def post_report_candidates(
        self,
        issuer_id: int,
        report_period_end: str,
        exclude_document_id: int | None = None,
    ) -> list[AnalysisCandidate]:
        """Köade pressmeddelanden publicerade EFTER rapportperiodens slut.

        Används för post-rapport-passet: finansieringshändelser (emission,
        lån) som skett efter rapportdatum ska extraheras så att runway kan
        justera kassan. Rapporten själv exkluderas.
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id AS document_id, d.issuer_id, i.canonical_name,
                       d.title, d.published_at, d.profile_category, d.language,
                       c.id AS content_id,
                       c.source_url, c.extracted_text, c.text_sha256,
                       c.page_count
                FROM documents d
                JOIN issuers i ON i.id=d.issuer_id
                JOIN document_contents c ON c.id=(
                    SELECT c2.id FROM document_contents c2
                    WHERE c2.document_id=d.id AND c2.status='extracted'
                      AND c2.extracted_text IS NOT NULL
                    ORDER BY
                        CASE
                            WHEN c2.quality_status='complete' THEN 0
                            WHEN c2.quality_status='not_required' THEN 1
                            ELSE 2
                        END,
                        c2.quality_score DESC,
                        c2.char_count DESC, c2.id
                    LIMIT 1
                )
                WHERE d.issuer_id=?
                  AND d.queued_for_analysis=1
                  AND d.profile_category!='report'
                  AND d.published_at>?
                  AND (? IS NULL OR d.id!=?)
                ORDER BY d.published_at ASC, d.id ASC
                """,
                (
                    issuer_id,
                    report_period_end,
                    exclude_document_id,
                    exclude_document_id,
                ),
            ).fetchall()
        return [
            AnalysisCandidate(
                document_id=int(row["document_id"]),
                issuer_id=int(row["issuer_id"]),
                issuer_name=str(row["canonical_name"]),
                title=str(row["title"]),
                published_at=str(row["published_at"]),
                profile_category=str(row["profile_category"]),
                language=row["language"],
                content_id=int(row["content_id"]),
                source_url=str(row["source_url"]),
                extracted_text=str(row["extracted_text"]),
                text_sha256=str(row["text_sha256"]),
                page_count=row["page_count"],
            )
            for row in rows
        ]

    def latest_report_period_end(self, issuer_id: int) -> str | None:
        """Senaste finansiella rapportens period-slut (YYYY-MM-DD) för bolaget.

        Primärt från ``report_extractions``. Om AI-extractionen av rapporten
        returnerade ``period_end: null`` (eller inte klassificerade dokumentet
        som finansiell rapport) saknas värdet där — då faller vi tillbaka på
        ``financial_facts.period_end``, som alltid sätts när en rapport faktiskt
        analyserats. Utan denna fallback skulle post-rapport-passet (som
        extraherar PM om emissioner/lån efter rapportdatum) skippas helt, och
        bolag som redan löst sin finansiering flaggas felaktigt för akut
        kapitalbehov.
        """
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT report_period_end
                FROM report_extractions
                WHERE issuer_id=?
                  AND is_financial_report=1
                  AND report_period_end IS NOT NULL
                ORDER BY report_period_end DESC, id DESC
                LIMIT 1
                """,
                (issuer_id,),
            ).fetchone()
            if row:
                return str(row["report_period_end"])
            fallback = connection.execute(
                """
                SELECT period_end
                FROM financial_facts
                WHERE issuer_id=? AND period_end IS NOT NULL
                ORDER BY period_end DESC, id DESC
                LIMIT 1
                """,
                (issuer_id,),
            ).fetchone()
        return str(fallback["period_end"]) if fallback else None

    def create_job(
        self,
        *,
        candidate: AnalysisCandidate,
        model: str,
        prompt_version: str,
        input_sha256: str,
        selected_char_count: int,
        estimated_input_tokens: int,
        deadline_at: str,
        provider: str,
    ) -> tuple[int, str, str | None]:
        lease_token = uuid4().hex
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, status, deadline_at
                FROM extraction_jobs
                WHERE document_id=? AND model=? AND prompt_version=?
                  AND input_sha256=?
                """,
                (
                    candidate.document_id,
                    model,
                    prompt_version,
                    input_sha256,
                ),
            ).fetchone()
            # Idempotency får inte låsa ett jobb som extraherade TOM output.
            # Ett jobb kan ha markerats 'completed' trots att det gav 0
            # financial_facts OCH 0 financing_events — t.ex. när det kördes på
            # en text som var NULL vid ett tidigare MFN-hämtningsfel. Då ska
            # jobbet återtas och köras om på den nu befintliga texten, annars
            # upptäcks aldrig genomförda emissioner/lån efter rapportdatum.
            # UNIQUE(document_id, model, prompt_version, input_sha256) gör att
            # vi återanvänder samma rad istället för att skapa en ny.
            if row is not None and str(row["status"]) == "completed":
                produced = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM financial_facts
                         WHERE extraction_job_id=?) +
                        (SELECT COUNT(*) FROM financing_event_evidence
                         WHERE extraction_job_id=?) AS produced
                    """,
                    (row["id"], row["id"]),
                ).fetchone()
                if produced is not None and int(produced["produced"]) == 0:
                    connection.execute(
                        """
                        UPDATE extraction_jobs
                        SET status='timed_out', lease_token=NULL,
                            error='Om-extrahering: tidigare extraction gav 0 facts',
                            completed_at=?
                        WHERE id=?
                        """,
                        (utc_now(), row["id"]),
                    )
                    row = dict(row)
                    row["status"] = "timed_out"
                else:
                    # Andra ofullständighetsfallet: dokumentet har minst ett
                    # event som beskriver bekräftat kassaflöde (confirmed_cash)
                    # utan att något belopp extraherades, och står kvar i ett
                    # preliminärt status (announced/approved/conditional). Det
                    # är signaturen för en utfallstext som extraherades med en
                    # äldre/svag prompt eller på ofullständig text: beskrivningen
                    # säger "genomfört/övertecknat" men status+belopp hänger inte
                    # med. Ett sådant event justerar aldrig kassan i runway och
                    # uppdateras aldrig av sig självt eftersom jobbet redan är
                    # idempotenta-completed. Återta jobbet så att det körs om
                    # med nuvarande prompt och text.
                    #
                    # Vi matchar på DOKUMENT-nivå (fee.document_id), inte på det
                    # specifika jobb-id:t: eventet kan ha producerats av ett
                    # äldre jobb (annan prompt_version/input_sha256) än det som
                    # nu matchar, och ska ändå trigga om-extrahering av detta
                    # dokument.
                    incomplete = connection.execute(
                        """
                        SELECT COUNT(*) AS n
                        FROM financing_events fe
                        JOIN financing_event_evidence fee
                          ON fee.financing_event_id = fe.id
                        WHERE fee.document_id = ?
                          AND fe.liquidity_effect_kind = 'confirmed_cash'
                          AND fe.cash_effect_amount IS NULL
                          AND fe.status IN ('announced', 'approved', 'conditional')
                        """,
                        (candidate.document_id,),
                    ).fetchone()
                    if incomplete is not None and int(incomplete["n"]) > 0:
                        connection.execute(
                            """
                            UPDATE extraction_jobs
                            SET status='timed_out', lease_token=NULL,
                                error='Om-extrahering: confirmed_cash-event saknar belopp',
                                completed_at=?
                            WHERE id=?
                            """,
                            (utc_now(), row["id"]),
                        )
                        row = dict(row)
                        row["status"] = "timed_out"
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO extraction_jobs (
                        issuer_id, document_id, content_id, provider, model,
                        prompt_version, input_sha256, selected_char_count,
                        estimated_input_tokens, status, lease_token, deadline_at,
                        attempt_count, started_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?,
                        1, ?
                    )
                    """,
                    (
                        candidate.issuer_id,
                        candidate.document_id,
                        candidate.content_id,
                        provider,
                        model,
                        prompt_version,
                        input_sha256,
                        selected_char_count,
                        estimated_input_tokens,
                        lease_token,
                        deadline_at,
                        now,
                    ),
                )
                return int(cursor.lastrowid), "created", lease_token
            job_id = int(row["id"])
            status = str(row["status"])
            if status == "completed":
                return job_id, status, None
            if status == "running":
                if row["deadline_at"] and str(row["deadline_at"]) <= now:
                    connection.execute(
                        """
                        UPDATE extraction_jobs
                        SET status='timed_out', lease_token=NULL,
                            completed_at=?,
                            error='AI-jobbet överskred sin absoluta deadline'
                        WHERE id=? AND status='running'
                        """,
                        (now, job_id),
                    )
                else:
                    return job_id, status, None
            connection.execute(
                """
                UPDATE extraction_jobs
                SET content_id=?, selected_char_count=?,
                    estimated_input_tokens=?, status='running',
                    lease_token=?, deadline_at=?,
                    attempt_count=attempt_count+1, response_id=NULL,
                    finish_reason=NULL, prompt_tokens=NULL,
                    completion_tokens=NULL, cached_tokens=NULL,
                    total_tokens=NULL, raw_response_json=NULL,
                    parsed_output_json=NULL, error=NULL,
                    started_at=?, completed_at=NULL
                WHERE id=?
                """,
                (
                    candidate.content_id,
                    selected_char_count,
                    estimated_input_tokens,
                    lease_token,
                    deadline_at,
                    now,
                    job_id,
                ),
            )
            return job_id, "reclaimed", lease_token

    def recover_stale_jobs(self) -> int:
        now = utc_now()
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET status='timed_out', lease_token=NULL,
                    error='AI-jobbet överskred sin absoluta deadline',
                    completed_at=?
                WHERE status='running' AND deadline_at IS NOT NULL
                  AND deadline_at<?
                """,
                (now, now),
            )
            return int(cursor.rowcount)

    def repair_ai_state(self) -> dict[str, int]:
        """Normalize legacy AI rows without discarding their audit history."""
        now = utc_now()
        with self.db.connect() as connection:
            completed_jobs = connection.execute(
                """
                UPDATE extraction_jobs
                SET error=NULL, lease_token=NULL
                WHERE status='completed'
                  AND (error IS NOT NULL OR lease_token IS NOT NULL)
                """
            ).rowcount
            orphaned_jobs = connection.execute(
                """
                UPDATE extraction_jobs
                SET status='timed_out', lease_token=NULL, completed_at=?,
                    error='AI-jobbet saknade en giltig deadline och avslutades vid datamigrering'
                WHERE status='running' AND deadline_at IS NULL
                """,
                (now,),
            ).rowcount
            expired_jobs = connection.execute(
                """
                UPDATE extraction_jobs
                SET status='timed_out', lease_token=NULL, completed_at=?,
                    error='AI-jobbet överskred sin absoluta deadline'
                WHERE status='running' AND deadline_at IS NOT NULL
                  AND deadline_at<?
                """,
                (now, now),
            ).rowcount
            orphaned_actions = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='timed_out', phase='timed_out', lease_token=NULL,
                    completed_at=?, updated_at=?,
                    error='Åtgärden saknade en giltig deadline och avslutades vid datamigrering'
                WHERE status IN ('queued', 'running') AND deadline_at IS NULL
                """,
                (now, now),
            ).rowcount
            expired_actions = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='timed_out', phase='timed_out', lease_token=NULL,
                    completed_at=?, updated_at=?,
                    error='Åtgärden överskred sin absoluta deadline'
                WHERE status IN ('queued', 'running')
                  AND deadline_at IS NOT NULL AND deadline_at<?
                """,
                (now, now, now),
            ).rowcount
            # Progress-watchdog: fanga actions som hanger i 'running' utan
            # nagon uppdatering pa >5 min (t.ex. worker som fastnat i
            # computing_runway vid serverinstabilitet), men som annu inte
            # natt sin absoluta deadline. Deadline-recovery ovan fangar dem
            # forst efter ~12 min — denna regel fangar dem tidigare.
            # Cutoff beraknas i Python (samma ISO-format som updated_at)
            # eftersom SQLite datetime() inte hanterar offset-suffixet.
            stalled_cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).replace(microsecond=0).isoformat()
            stalled_actions = connection.execute(
                """
                UPDATE manual_action_runs
                SET status='timed_out', phase='timed_out', lease_token=NULL,
                    completed_at=?, updated_at=?,
                    error='Ingen progress inom 5 min (worker hängde?)'
                WHERE status='running'
                  AND updated_at IS NOT NULL
                  AND updated_at < ?
                  AND (deadline_at IS NULL OR deadline_at >= ?)
                """,
                (now, now, stalled_cutoff, now),
            ).rowcount
            terminal_actions = connection.execute(
                """
                UPDATE manual_action_runs
                SET lease_token=NULL,
                    error=CASE
                        WHEN status IN ('completed', 'insufficient_facts')
                        THEN NULL ELSE error
                    END,
                    phase=CASE
                        WHEN status IN (
                            'completed', 'insufficient_facts',
                            'failed', 'timed_out'
                        ) THEN status
                        ELSE phase
                    END,
                    updated_at=COALESCE(updated_at, completed_at, started_at)
                WHERE status NOT IN ('queued', 'running')
                  AND (
                      lease_token IS NOT NULL
                      OR (
                          status IN ('completed', 'insufficient_facts')
                          AND error IS NOT NULL
                      )
                      OR phase!=status
                      OR updated_at IS NULL
                  )
                """
            ).rowcount
        return {
            "completed_jobs_normalized": int(completed_jobs),
            "orphaned_jobs_timed_out": int(orphaned_jobs),
            "expired_jobs_timed_out": int(expired_jobs),
            "orphaned_actions_timed_out": int(orphaned_actions),
            "expired_actions_timed_out": int(expired_actions),
            "stalled_actions_timed_out": int(stalled_actions),
            "terminal_actions_normalized": int(terminal_actions),
        }

    def fail_job(
        self,
        job_id: int,
        lease_token: str,
        error: str,
        *,
        status: str = "failed",
    ) -> bool:
        if status not in {"failed", "timed_out"}:
            raise ValueError(f"Ogiltig felstatus: {status}")
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE extraction_jobs SET status=?, error=?, completed_at=?,
                    lease_token=NULL
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    status,
                    error[:4000],
                    utc_now(),
                    job_id,
                    lease_token,
                ),
            )
            return cursor.rowcount == 1

    def complete_job(
        self,
        job_id: int,
        lease_token: str,
        *,
        response_id: str | None,
        finish_reason: str | None,
        usage: dict[str, int | None],
        raw_response: dict[str, Any],
        parsed_output: dict[str, Any],
    ) -> bool:
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE extraction_jobs
                SET status='completed', response_id=?, finish_reason=?,
                    prompt_tokens=?, completion_tokens=?, cached_tokens=?,
                    total_tokens=?, raw_response_json=?, parsed_output_json=?,
                    completed_at=?, error=NULL, lease_token=NULL
                WHERE id=? AND status='running' AND lease_token=?
                """,
                (
                    response_id,
                    finish_reason,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("cached_tokens"),
                    usage.get("total_tokens"),
                    json.dumps(raw_response, ensure_ascii=False),
                    json.dumps(parsed_output, ensure_ascii=False),
                    utc_now(),
                    job_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._persist_extraction(connection, job_id, parsed_output)
            return True

    def _persist_extraction(
        self,
        connection: sqlite3.Connection,
        job_id: int,
        output: dict[str, Any],
    ) -> None:
        job = connection.execute(
            "SELECT issuer_id, document_id FROM extraction_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        issuer_id = int(job["issuer_id"])
        document_id = int(job["document_id"])
        now = utc_now()
        report = output["report"]
        quality = output["quality"]
        connection.execute(
            """
            INSERT INTO report_extractions (
                issuer_id, document_id, extraction_job_id, is_financial_report,
                report_period_start, report_period_end, publication_date,
                currency, reported_scale, period_months, confidence,
                requires_human_review, uncertainties_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                extraction_job_id=excluded.extraction_job_id,
                is_financial_report=excluded.is_financial_report,
                report_period_start=excluded.report_period_start,
                report_period_end=excluded.report_period_end,
                publication_date=excluded.publication_date,
                currency=excluded.currency,
                reported_scale=excluded.reported_scale,
                period_months=excluded.period_months,
                confidence=excluded.confidence,
                requires_human_review=excluded.requires_human_review,
                uncertainties_json=excluded.uncertainties_json,
                updated_at=excluded.updated_at
            """,
            (
                issuer_id,
                document_id,
                job_id,
                int(report["is_financial_report"]),
                report["period_start"],
                report["period_end"],
                report["publication_date"],
                report["currency"],
                report["reported_scale"],
                report["period_months"],
                report["confidence"],
                int(quality["requires_human_review"]),
                json.dumps(quality["uncertainties"], ensure_ascii=False),
                now,
                now,
            ),
        )
        connection.execute("DELETE FROM financial_facts WHERE document_id=?", (document_id,))
        for fact in report["facts"]:
            connection.execute(
                """
                INSERT INTO financial_facts (
                    issuer_id, document_id, extraction_job_id, fact_type,
                    reported_value, normalized_value, currency, reported_unit,
                    period_start, period_end, as_of_date, evidence_label,
                    source_page, source_excerpt, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issuer_id, document_id, job_id, fact["fact_type"],
                    fact["reported_value"], fact["normalized_value"],
                    fact["currency"], fact["reported_unit"],
                    fact["period_start"], fact["period_end"], fact["as_of_date"],
                    fact["evidence_label"], fact["source_page"],
                    fact["source_excerpt"], fact["confidence"], now,
                ),
            )
        tranche_store = FinancingHistoryStore(self.db)
        for event in output["financing_events"]:
            if event.get("credit_limit") is not None:
                if event.get("drawn_amount") is None:
                    event["cash_effect_amount"] = 0.0
                    if event.get("liquidity_effect_kind") == "confirmed_cash":
                        event["liquidity_effect_kind"] = "committed_liquidity"
                elif event.get("cash_effect_amount") is None:
                    event["cash_effect_amount"] = event.get("drawn_amount")
                if event.get("undrawn_commitment") is None and event.get("drawn_amount") is not None:
                    event["undrawn_commitment"] = max(
                        0.0, float(event["credit_limit"]) - float(event["drawn_amount"])
                    )
            fingerprint = stable_hash(
                str(issuer_id),
                event["event_type"],
                event["effective_date"] or event["announcement_date"],
                event["currency"],
                str(
                    event["cash_effect_amount"]
                    or event["net_amount"]
                    or event["gross_amount"]
                    or event["committed_amount"]
                    or event["conditional_amount"]
                    or ""
                ),
            )
            if event.get("canonical_round_key") and event.get("lifecycle_stage"):
                fingerprint = stable_hash(
                    fingerprint,
                    str(event["canonical_round_key"]),
                    str(event["lifecycle_stage"]),
                )
            connection.execute(
                """
                INSERT INTO financing_events (
                    issuer_id, event_fingerprint, event_type, description,
                    announcement_date, effective_date, status, cash_direction,
                    gross_amount, net_amount, cash_effect_amount, currency,
                    liquidity_effect_kind, committed_amount, conditional_amount,
                    reflected_in_report, conditions, use_of_proceeds, confidence,
                    canonical_round_key, lifecycle_stage, stage_effective_at,
                    issue_price, subscription_commitment_amount,
                    subscription_commitment_pct, guarantee_commitment_amount,
                    guarantee_commitment_pct, guarantee_utilized_amount,
                    guarantee_utilized_pct, guarantee_fee_details,
                    transaction_cost, adviser, issuer_agent, outcome,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_fingerprint) DO UPDATE SET
                    description=excluded.description,
                    status=excluded.status,
                    cash_effect_amount=COALESCE(excluded.cash_effect_amount, cash_effect_amount),
                    committed_amount=COALESCE(excluded.committed_amount, committed_amount),
                    conditional_amount=COALESCE(excluded.conditional_amount, conditional_amount),
                    canonical_round_key=COALESCE(excluded.canonical_round_key, canonical_round_key),
                    lifecycle_stage=COALESCE(excluded.lifecycle_stage, lifecycle_stage),
                    stage_effective_at=COALESCE(excluded.stage_effective_at, stage_effective_at),
                    issue_price=COALESCE(excluded.issue_price, issue_price),
                    subscription_commitment_amount=COALESCE(excluded.subscription_commitment_amount, subscription_commitment_amount),
                    subscription_commitment_pct=COALESCE(excluded.subscription_commitment_pct, subscription_commitment_pct),
                    guarantee_commitment_amount=COALESCE(excluded.guarantee_commitment_amount, guarantee_commitment_amount),
                    guarantee_commitment_pct=COALESCE(excluded.guarantee_commitment_pct, guarantee_commitment_pct),
                    guarantee_utilized_amount=COALESCE(excluded.guarantee_utilized_amount, guarantee_utilized_amount),
                    guarantee_utilized_pct=COALESCE(excluded.guarantee_utilized_pct, guarantee_utilized_pct),
                    guarantee_fee_details=COALESCE(excluded.guarantee_fee_details, guarantee_fee_details),
                    transaction_cost=COALESCE(excluded.transaction_cost, transaction_cost),
                    adviser=COALESCE(excluded.adviser, adviser),
                    issuer_agent=COALESCE(excluded.issuer_agent, issuer_agent),
                    outcome=COALESCE(excluded.outcome, outcome),
                    reflected_in_report=excluded.reflected_in_report,
                    confidence=MAX(confidence, excluded.confidence),
                    updated_at=excluded.updated_at
                """,
                (
                    issuer_id, fingerprint, event["event_type"], event["description"],
                    event["announcement_date"], event["effective_date"], event["status"],
                    event["cash_direction"], event["gross_amount"], event["net_amount"],
                    event["cash_effect_amount"], event["currency"],
                    event["liquidity_effect_kind"], event["committed_amount"],
                    event["conditional_amount"], event["reflected_in_report"],
                    event["conditions"], event["use_of_proceeds"], event["confidence"],
                    event.get("canonical_round_key"), event.get("lifecycle_stage"),
                    event.get("stage_effective_at"), event.get("issue_price"),
                    event.get("subscription_commitment_amount"), event.get("subscription_commitment_pct"),
                    event.get("guarantee_commitment_amount"), event.get("guarantee_commitment_pct"),
                    event.get("guarantee_utilized_amount"), event.get("guarantee_utilized_pct"),
                    event.get("guarantee_fee_details"), event.get("transaction_cost"),
                    event.get("adviser"), event.get("issuer_agent"), event.get("outcome"),
                    now, now,
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
                (
                    event.get("guarantee_fee_amount"),
                    event.get("guarantee_fee_pct"),
                    event.get("guarantee_fee_cash_pct"),
                    event.get("guarantee_fee_share_pct"),
                    event.get("guarantee_fee_basis"),
                    event.get("credit_limit"),
                    event.get("drawn_amount"),
                    event.get("undrawn_commitment"),
                    fingerprint,
                ),
            )
            event_row = connection.execute(
                "SELECT id FROM financing_events WHERE event_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            # The legacy persist route is still used by completed extraction
            # jobs, so it stores the same source-bound guarantee layers as V2.
            # A total guarantee is never split between tiers here.
            for raw_tranche in event.get("guarantee_tranches") or []:
                if not isinstance(raw_tranche, dict):
                    continue
                tier = str(raw_tranche.get("tier") or "").strip().casefold()
                if tier not in {"top", "middle", "bottom", "other"}:
                    continue
                tranche_store.record_guarantee_tranche(
                    connection,
                    GuaranteeTranche(
                        financing_event_id=int(event_row["id"]),
                        tier=tier,
                        commitment_amount=raw_tranche.get("commitment_amount"),
                        fee_rate=raw_tranche.get("fee_rate"),
                        fee_amount=raw_tranche.get("fee_amount"),
                        utilized_amount=raw_tranche.get("utilized_amount"),
                        source_document_id=document_id,
                        source_page=raw_tranche.get("source_page"),
                        source_excerpt=raw_tranche.get("source_excerpt") or None,
                        confidence=float(raw_tranche.get("confidence") or 0.0),
                    ),
                )
            connection.execute(
                """
                INSERT INTO financing_event_evidence (
                    financing_event_id, document_id, extraction_job_id,
                    evidence_label, source_page, source_excerpt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(financing_event_id, document_id) DO UPDATE SET
                    extraction_job_id=excluded.extraction_job_id,
                    evidence_label=excluded.evidence_label,
                    source_page=excluded.source_page,
                    source_excerpt=excluded.source_excerpt
                """,
                (
                    int(event_row["id"]), document_id, job_id,
                    event["evidence_label"], event["source_page"],
                    event["source_excerpt"], now,
                ),
            )
        connection.execute(
            "UPDATE documents SET analysis_status='extracted', updated_at=? WHERE id=?",
            (now, document_id),
        )

    def purge_document_raw_content(self, issuer_id: int) -> dict[str, int]:
        """Radera tung rådata (PDF-fil + extraherad text) för en issuers dokument.

        Körs efter att hela analyskedjan är klar. MFN-källan är lätt att
        ladda om via source_url, så vi sparar bara den strukturerade datan
        (financial_facts, financing_events, artifacts, tolkningar). Raderar
        ALDRIG source_url, document-metadata eller evidence-kopplingar.
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.local_path
                FROM document_contents c
                JOIN documents d ON d.id=c.document_id
                WHERE d.issuer_id=?
                  AND (c.local_path IS NOT NULL OR c.extracted_text IS NOT NULL)
                """,
                (issuer_id,),
            ).fetchall()
            files_deleted = 0
            for row in rows:
                local_path = row["local_path"]
                if local_path and os.path.isfile(local_path):
                    try:
                        os.remove(local_path)
                        files_deleted += 1
                    except OSError:
                        pass
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE document_contents
                SET local_path=NULL, extracted_text=NULL, byte_size=NULL,
                    char_count=0, updated_at=?
                WHERE id IN (
                    SELECT c.id FROM document_contents c
                    JOIN documents d ON d.id=c.document_id
                    WHERE d.issuer_id=?
                )
                """,
                (now, issuer_id),
            )
            connection.commit()
            return {
                "contents_purged": int(cursor.rowcount or 0),
                "files_deleted": files_deleted,
            }

    def purge_single_document_content(self, document_id: int) -> dict[str, int]:
        """Radera tung rådata (PDF-fil + extraherad text) för ETT dokument.

        Körs direkt efter att extraction persisterat financial_facts och
        financing_events — då finns all strukturerad data kvar och råtexten
        behövs inte längre. Till skillnad från purge_document_raw_content
        (som rensar en hel issuers dokument efter hela tolkningskedjan)
        rensar denna bara det dokument som just analyserats, så att
        parallella workers som bearbetar andra dokument inte påverkas.
        Raderar ALDRIG source_url, document-metadata eller evidence.
        """
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, local_path FROM document_contents
                WHERE document_id=?
                  AND (local_path IS NOT NULL OR extracted_text IS NOT NULL)
                """,
                (document_id,),
            ).fetchall()
            files_deleted = 0
            for row in rows:
                local_path = row["local_path"]
                if local_path and os.path.isfile(local_path):
                    try:
                        os.remove(local_path)
                        files_deleted += 1
                    except OSError:
                        pass
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE document_contents
                SET local_path=NULL, extracted_text=NULL, byte_size=NULL,
                    char_count=0, updated_at=?
                WHERE document_id=?
                """,
                (now, document_id),
            )
            connection.commit()
            return {
                "contents_purged": int(cursor.rowcount or 0),
                "files_deleted": files_deleted,
            }

    def purge_raw_content_bulk(
        self,
        older_than_days: int = 7,
        include_extracted: bool = True,
    ) -> dict[str, int]:
        """Radera tung rådata (PDF-fil + extraherad text) i bulk.

        Rensar dokument som antingen:
        - har analysis_status='extracted' (redan analyserade, rådata behövs ej), ELLER
        - är äldre än older_than_days och inte är content_ready (kommer ej analyseras).

        Raderar ALDRIG source_url, document-metadata eller evidence-kopplingar.
        Returnerar statistik över rensningen.
        """
        cutoff = utc_now()[:10]  # YYYY-MM-DD
        if older_than_days > 0:
            from datetime import datetime, timedelta
            cutoff_date = datetime.strptime(cutoff, "%Y-%m-%d") - timedelta(days=older_than_days)
            cutoff = cutoff_date.strftime("%Y-%m-%d")

        with self.db.connect() as connection:
            # Hämta alla dokument som ska rensas
            rows = connection.execute(
                """
                SELECT c.id, c.local_path, d.analysis_status, d.published_at
                FROM document_contents c
                JOIN documents d ON d.id = c.document_id
                WHERE (c.local_path IS NOT NULL OR c.extracted_text IS NOT NULL)
                  AND (
                    (? AND d.analysis_status = 'extracted')
                    OR (d.published_at < ? AND d.analysis_status != 'content_ready')
                  )
                """,
                (include_extracted, cutoff),
            ).fetchall()

            files_deleted = 0
            for row in rows:
                local_path = row["local_path"]
                if local_path and os.path.isfile(local_path):
                    try:
                        os.remove(local_path)
                        files_deleted += 1
                    except OSError:
                        pass

            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE document_contents
                SET local_path=NULL, extracted_text=NULL, byte_size=NULL,
                    char_count=0, updated_at=?
                WHERE id IN (
                    SELECT c.id FROM document_contents c
                    JOIN documents d ON d.id = c.document_id
                    WHERE (c.local_path IS NOT NULL OR c.extracted_text IS NOT NULL)
                      AND (
                        (? AND d.analysis_status = 'extracted')
                        OR (d.published_at < ? AND d.analysis_status != 'content_ready')
                      )
                )
                """,
                (now, include_extracted, cutoff),
            )
            connection.commit()
            return {
                "contents_purged": int(cursor.rowcount or 0),
                "files_deleted": files_deleted,
                "cutoff_date": cutoff,
                "include_extracted": include_extracted,
            }

    def phase3_status(self) -> dict[str, Any]:
        with self.db.connect() as connection:
            content = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM document_contents GROUP BY status ORDER BY status
                """
            ).fetchall()
            jobs = connection.execute(
                """
                SELECT status, COUNT(*) AS count,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM extraction_jobs GROUP BY status ORDER BY status
                """
            ).fetchall()
            latest = connection.execute(
                """
                SELECT i.canonical_name, r.adjusted_runway_months,
                       r.adjusted_need_score, r.need_timing, r.alert_level,
                       r.evidence_posture
                FROM runway_assessments r
                JOIN issuers i ON i.id=r.issuer_id
                WHERE r.id=(
                    SELECT r2.id FROM runway_assessments r2
                    WHERE r2.issuer_id=r.issuer_id
                    ORDER BY r2.id DESC LIMIT 1
                )
                ORDER BY r.adjusted_need_score DESC, i.canonical_name
                """
            ).fetchall()
        return {
            "document_contents": [dict(row) for row in content],
            "extraction_jobs": [dict(row) for row in jobs],
            "runway": [dict(row) for row in latest],
        }

    def set_watchlist(
        self,
        issuer_id: int,
        *,
        active: bool = True,
        mfn_refreshed: bool = False,
        interpreted: bool = False,
    ) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM issuers WHERE id=?", (issuer_id,)
            ).fetchone():
                raise KeyError(f"Okänt bolags-ID: {issuer_id}")
            connection.execute(
                """
                INSERT INTO issuer_watchlist (
                    issuer_id, active, added_at, updated_at,
                    last_mfn_refresh_at, last_interpretation_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(issuer_id) DO UPDATE SET
                    active=excluded.active,
                    updated_at=excluded.updated_at,
                    last_mfn_refresh_at=CASE
                        WHEN ? THEN excluded.updated_at
                        ELSE issuer_watchlist.last_mfn_refresh_at END,
                    last_interpretation_at=CASE
                        WHEN ? THEN excluded.updated_at
                        ELSE issuer_watchlist.last_interpretation_at END
                """,
                (
                    issuer_id,
                    int(active),
                    now,
                    now,
                    now if mfn_refreshed else None,
                    now if interpreted else None,
                    int(mfn_refreshed),
                    int(interpreted),
                ),
            )

    def save_interpretation(self, item: dict[str, Any]) -> bool:
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO capital_need_interpretations (
                    issuer_id, as_of_date, method, need_level, need_score,
                    need_timing, headline, summary, capital_need,
                    transaction_context, recommended_action, cash_amount,
                    operating_cash_flow, period_months, currency,
                    report_period_end, confidence, evidence_posture,
                    evidence_json, source_document_ids_json,
                    interpretation_sha256, created_at
                ) VALUES (
                    :issuer_id, :as_of_date, :method, :need_level, :need_score,
                    :need_timing, :headline, :summary, :capital_need,
                    :transaction_context, :recommended_action, :cash_amount,
                    :operating_cash_flow, :period_months, :currency,
                    :report_period_end, :confidence, :evidence_posture,
                    :evidence_json, :source_document_ids_json,
                    :interpretation_sha256, :created_at
                )
                """,
                {**item, "created_at": now},
            )
            created = cursor.rowcount == 1
            interpretation = connection.execute(
                """
                SELECT *
                FROM capital_need_interpretations
                WHERE interpretation_sha256=?
                """,
                (item["interpretation_sha256"],),
            ).fetchone()
            if not interpretation:
                raise RuntimeError("Den sparade kapitalbehovstolkningen saknas")
            existing_version = connection.execute(
                """
                SELECT id
                FROM analysis_versions
                WHERE interpretation_id=?
                """,
                (interpretation["id"],),
            ).fetchone()
            if existing_version is None:
                issuer_id = int(item["issuer_id"])
                next_version = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(version_no), 0) + 1
                        FROM analysis_versions WHERE issuer_id=?
                        """,
                        (issuer_id,),
                    ).fetchone()[0]
                )
                runway = connection.execute(
                    """
                    SELECT id, adjusted_runway_months
                    FROM runway_assessments
                    WHERE issuer_id=? AND report_period_end=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (issuer_id, item.get("report_period_end")),
                ).fetchone()
                action = connection.execute(
                    """
                    SELECT id
                    FROM manual_action_runs
                    WHERE issuer_id=?
                      AND action_type='interpret_capital_need'
                      AND status IN ('queued', 'running')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (issuer_id,),
                ).fetchone()
                need_score = int(item["need_score"])
                # "Kapitalbehov" i radarn ska betyda ett identifierat behov
                # inom bevakningshorisonten, inte att ett bolag med normal
                # negativ burn någon gång i framtiden kan behöva kapital.
                decision = (
                    "yes"
                    if need_score > 0
                    and item["need_timing"] not in {
                        "no_negative_operating_cash_flow_signal",
                        "more_than_24_months",
                    }
                    else "no"
                )
                version_cursor = connection.execute(
                    """
                    INSERT INTO analysis_versions (
                        issuer_id, version_no, interpretation_id,
                        runway_assessment_id, action_id, status,
                        capital_need_decision, need_score, need_timing,
                        runway_months, alert_level, bergs_fit_score,
                        bergs_fit_summary, headline, confidence,
                        source_as_of_date, source_document_ids_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?)
                    """,
                    (
                        issuer_id,
                        next_version,
                        interpretation["id"],
                        runway["id"] if runway else None,
                        action["id"] if action else None,
                        decision,
                        need_score,
                        item["need_timing"],
                        runway["adjusted_runway_months"] if runway else None,
                        item["need_level"],
                        item.get("bergs_fit_score"),
                        item.get("bergs_fit_summary"),
                        item["headline"],
                        item["confidence"],
                        item["as_of_date"],
                        item["source_document_ids_json"],
                        now,
                    ),
                )
                version_id = int(version_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO issuer_analysis_summary (
                        issuer_id, current_analysis_version_id,
                        current_interpretation_id,
                        current_runway_assessment_id, last_action_id,
                        analysis_status, capital_need_decision, need_score,
                        need_timing, runway_months, alert_level,
                        bergs_fit_score, headline, confidence,
                        source_as_of_date, last_success_at, last_attempt_at,
                        failure_reason, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, NULL, ?
                    )
                    ON CONFLICT(issuer_id) DO UPDATE SET
                        current_analysis_version_id=
                            excluded.current_analysis_version_id,
                        current_interpretation_id=
                            excluded.current_interpretation_id,
                        current_runway_assessment_id=
                            excluded.current_runway_assessment_id,
                        last_action_id=excluded.last_action_id,
                        analysis_status='completed',
                        capital_need_decision=excluded.capital_need_decision,
                        need_score=excluded.need_score,
                        need_timing=excluded.need_timing,
                        runway_months=excluded.runway_months,
                        alert_level=excluded.alert_level,
                        bergs_fit_score=excluded.bergs_fit_score,
                        headline=excluded.headline,
                        confidence=excluded.confidence,
                        source_as_of_date=excluded.source_as_of_date,
                        last_success_at=excluded.last_success_at,
                        last_attempt_at=excluded.last_attempt_at,
                        failure_reason=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        issuer_id,
                        version_id,
                        interpretation["id"],
                        runway["id"] if runway else None,
                        action["id"] if action else None,
                        decision,
                        need_score,
                        item["need_timing"],
                        runway["adjusted_runway_months"] if runway else None,
                        item["need_level"],
                        item.get("bergs_fit_score"),
                        item["headline"],
                        item["confidence"],
                        item["as_of_date"],
                        now,
                        now,
                        now,
                    ),
                )
        self.set_watchlist(
            int(item["issuer_id"]), active=True, interpreted=True
        )
        return created


def _document_family(title: str) -> str:
    value = title.casefold()
    if any(term in value for term in ("rapport", "report", "bokslut", "year-end")):
        return "report"
    if any(
        term in value
        for term in (
            "emission", "issue", "finansier", "financing", "loan", "lån",
            "facility", "kredit",
        )
    ):
        return "financing"
    if any(term in value for term in ("förvärv", "acquisition", "fusion", "merger")):
        return "transaction"
    return "other"
