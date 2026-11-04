"""Additive pro forma analysis. Never writes to existing screening tables."""
from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import sqlite3
import logging
import threading
from datetime import date, datetime, timezone
from typing import Any

from .transaction_feed import _RX, _transaction_items, _money

VERSION = "radar-pro-forma-v4"
# Status där kassan anses inbetald; annonserade affärer viktas 0 tills någon
# sätter en vikt manuellt.
CONFIRMED_STATUSES = frozenset({"completed", "registered", "preliminary_outcome"})
DAYS_PER_MONTH = 30.4375
UNKNOWN_CASH_REASON = "Mottagen kontantlikvid är okänd"
BANDS = ("0–3", "3–6", "6–12", "12–18", "18+")
SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_financing_weights (
 id INTEGER PRIMARY KEY, issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 transaction_id TEXT NOT NULL, weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS radar_weight_latest
 ON radar_financing_weights(transaction_id, id);
CREATE TABLE IF NOT EXISTS issuer_runway_snapshots (
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), snapshot_date TEXT NOT NULL,
 reported_runway_months REAL, adjusted_runway_months REAL, runway_band TEXT,
 cash REAL, monthly_burn REAL, calculation_version TEXT NOT NULL,
 calculation_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(issuer_id, snapshot_date)
);
CREATE TRIGGER IF NOT EXISTS radar_snapshot_no_update
 BEFORE UPDATE ON issuer_runway_snapshots BEGIN
 SELECT RAISE(ABORT, 'runway snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS radar_snapshot_no_delete
 BEFORE DELETE ON issuer_runway_snapshots BEGIN
 SELECT RAISE(ABORT, 'runway snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS adjusted_runway_recalculations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 calculation_hash TEXT NOT NULL,
 correction_ids_json TEXT NOT NULL,
 before_json TEXT NOT NULL,
 after_json TEXT NOT NULL,
 actor TEXT NOT NULL,
 reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
 created_at TEXT NOT NULL,
 UNIQUE(issuer_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS adjusted_runway_recalculations_issuer
 ON adjusted_runway_recalculations(issuer_id, id DESC);
CREATE TRIGGER IF NOT EXISTS adjusted_runway_recalculations_no_update
 BEFORE UPDATE ON adjusted_runway_recalculations BEGIN
 SELECT RAISE(ABORT, 'runway recalculation audits are immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjusted_runway_recalculations_no_delete
 BEFORE DELETE ON adjusted_runway_recalculations BEGIN
 SELECT RAISE(ABORT, 'runway recalculation audits are immutable'); END;
"""


def runway_band(value: float | None, *, positive: bool = False) -> str | None:
    if positive:
        return "cash_flow_positive"
    if value is None:
        return None
    for limit, label in zip((3, 6, 12, 18), BANDS):
        if value < limit:
            return label
    return "18+"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _amount_label(value: float, currency: str | None) -> str:
    unit = currency or ""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:g} M{unit}".replace(".", ",").strip()
    return f"{value:g} {unit}".replace(".", ",").strip()


def _transaction_text(tx: dict) -> str:
    """Keep safety checks inside the current financing's category context."""
    titles = " ".join(str(item.get("title", "")) for item in tx.get("history") or [])
    source = str(tx.get("_source_text") or "")
    category = _RX.get(str(tx.get("category") or ""))
    if category is None:
        return f"{titles} {source}".strip()
    sentences = re.split(r"(?<=[.!?;])\s+", source)
    relevant: list[str] = []
    for index, sentence in enumerate(sentences):
        if not category.search(sentence):
            continue
        relevant.append(sentence)
        if index + 1 < len(sentences):
            continuation = sentences[index + 1]
            other_category = any(
                key != str(tx.get("category") or "") and pattern.search(continuation)
                for key, pattern in _RX.items()
            )
            if not other_category:
                relevant.append(continuation)
    scoped_source = " ".join(relevant) if relevant else source
    return f"{titles} {scoped_source}".strip()


def _cash_amount(tx: dict, details: dict, text: str) -> tuple[float | None, str | None, str | None]:
    """Resolve a round's cash effect without treating other instruments as cash.

    A reviewed transaction-level cash value is authoritative, including zero
    or an explicit reviewed unknown.
    Older transactions retain their advertised pro forma amount only where the
    source does not identify a non-cash or conditional component.
    """
    effective = tx.get("effective_transaction_values") or {}
    original = tx.get("original_transaction_values") or {}
    basis = effective.get("amount_basis") or tx.get("amount_basis")
    corrected_fields = set(tx.get("effective_corrected_fields") or [])
    reviewed_cash = (
        "confirmed_cash_effect_amount" in corrected_fields
        or ("confirmed_cash_effect_amount" in effective
            and original.get("confirmed_cash_effect_amount") is not None
            and effective["confirmed_cash_effect_amount"]
            != original["confirmed_cash_effect_amount"])
    )
    # Explicit reviewed unknown is authoritative before legacy amount,
    # non-cash text or credit/conditional fallbacks. It is never a receipt of zero.
    if reviewed_cash and effective.get("confirmed_cash_effect_amount") is None:
        return None, "confirmed_cash_effect_amount", UNKNOWN_CASH_REASON
    if tx.get("category") == "credit_facility":
        dossier = details.get("financing_dossier") or {}
        drawn = _number(dossier.get("drawn_amount"))
        if drawn is not None:
            return drawn, "drawn_amount", None
        return 0.0, "credit_limit", "Kreditram utan verifierat uttag"
    if basis == "guarantee_commitment":
        return 0.0, basis, "Garantiåtagande är inte en separat kassainbetalning"
    if basis in {"conditional_liquidity", "conditional_tranche", "future_warrant_proceeds"}:
        return 0.0, basis, "Villkorad framtida likviditet"
    if (tx.get("category") == "warrant_exercise"
            and tx.get("status") not in {"completed", "registered"}):
        return 0.0, basis, "Framtida optionsutfall är villkorat"
    sources = ([effective] if reviewed_cash else []) + [tx]
    for source in sources:
        if source.get("confirmed_cash_effect_amount") is not None:
            amount = _number(source["confirmed_cash_effect_amount"])
            if amount is None or amount < 0:
                return None, "confirmed_cash_effect_amount", "Ogiltig bekräftad kassaeffekt"
            return amount, "confirmed_cash_effect_amount", None
    if re.search(
        r"kvittning|apport|konvertering|conversion of|ingen kontant likvid|"
        r"utan kontant (?:betalning|likvid)|no cash (?:consideration|proceeds)",
        text, re.I,
    ):
        return 0.0, basis, "Icke-kontant finansiering"
    if tx.get("category") in {"bridge_loan", "shareholder_loan", "other_debt", "credit_facility"}:
        if re.search(r"delvis utbetald|hittills utbetalt|partly drawn|partially drawn|undrawn", text, re.I):
            return 0.0, basis, "Utbetalat lånebelopp saknas"
    if tx.get("status") in {"completed", "registered"}:
        sourced_cash = _number(details.get("confirmed_cash_effect_amount"))
        if sourced_cash is not None and sourced_cash > 0:
            return sourced_cash, "confirmed_cash_effect_amount", None
    for field in ("outcome_amount", "announced_amount"):
        amount = _number(details.get(field))
        if amount is not None:
            return amount, details.get(field + "_basis"), None
    return _number(tx.get("amount")), basis, None


def _months_between(start: str | None, end: str | None) -> float:
    try:
        days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, days / DAYS_PER_MONTH)


def calculate(assessment: dict, transactions: list[dict], weights: dict,
              *, as_of: str) -> dict:
    """Runway från idag: rapporterad kassa rullas fram med burn till `as_of`,
    plus post-report-finansiering räknad en gång.

    Endast genomförd finansiering räknas som kassa som standard; annonserade
    belopp får vikt 0 tills en manuell vikt sätts. Bruttobelopp är fortsatt
    före kostnader. Icke-kontant kapital och okända valutor är aldrig kassa.
    """
    cash = _number(assessment.get("baseline_cash"))
    burn = _number(assessment.get("monthly_operating_burn"))
    operating_flow = _number(assessment.get("operating_cash_flow"))
    positive = operating_flow is not None and operating_flow >= 0
    reported = _number(assessment.get("operating_runway_months"))
    period = assessment.get("report_period_end")
    currency = assessment.get("currency")
    months_since_report = _months_between(period, as_of)
    rolled_cash = (cash - burn * months_since_report
                   if cash is not None and burn is not None and burn > 0 else cash)
    events = []
    seen = set()
    seen_rounds = set()
    for tx in transactions:
        if tx["issuer_id"] != assessment.get("issuer_id"):
            continue
        if tx["id"] in seen:
            continue
        seen.add(tx["id"])
        history = tx.get("history") or []
        first = min((h.get("published_at", "")[:10] for h in history if h.get("published_at")), default=tx.get("published_at", "")[:10])
        if not period or not first or first <= period or first > as_of:
            continue
        details = tx.get("details") or {}
        dossier = details.get("financing_dossier") or {}
        round_keys = dossier.get("canonical_round_keys") or []
        if len(round_keys) == 1:
            round_key = str(round_keys[0]).strip().casefold()
            round_identity = (tx.get("issuer_id"), round_key)
            if round_identity in seen_rounds:
                continue
            seen_rounds.add(round_identity)
        text = _transaction_text(tx)
        amount, basis, cash_reason = _cash_amount(tx, details, text)
        reason = None
        if any(h.get("published_at", "")[:10] > as_of for h in history):
            reason = "Transaktionen innehåller senare information"
        elif cash_reason:
            reason = cash_reason
        elif tx.get("status") in {"cancelled", "withdrawn", "terminated"}:
            reason = "Avbruten finansiering"
        elif len({c for _, _, c in _money(text)}) > 1 or currency is None or tx.get("currency") != currency:
            reason = "Valutan kan inte förenas med rapporten"
        elif amount is None or amount < 0:
            reason = "Belopp saknas eller kan inte användas"
        if tx["id"] in weights:
            weight, weight_source = weights[tx["id"]], "manual"
        elif tx.get("status") in CONFIRMED_STATUSES:
            weight, weight_source = 1.0, "default_confirmed"
        else:
            weight, weight_source = 0.0, "default_unconfirmed"
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("Vikten måste vara 0–100 %")
        effect = (None if cash_reason == UNKNOWN_CASH_REASON
                  else 0.0 if reason else amount * weight)
        events.append({"transaction_id": tx["id"], "category": tx["category"],
                       "category_label": tx.get("category_label"), "status": tx.get("status"),
                       "announcement_date": first, "amount": amount, "amount_basis": basis,
                       "currency": tx.get("currency"), "weight": weight,
                       "weight_source": weight_source,
                       "cash_effect": effect, "exclusion_reason": reason,
                       "correction_ids": tx.get("applied_transaction_correction_ids", []),
                       "source_document_ids": tx.get("source_document_ids", [])})
    adjustment = sum(e["cash_effect"] for e in events if e["cash_effect"] is not None)
    has_burn = cash is not None and burn is not None and burn > 0
    adjusted = round(max(0, rolled_cash + adjustment) / burn, 1) if has_burn else None
    from_today = round(max(0, rolled_cash) / burn, 1) if has_burn else None
    included_events = [event for event in events if event["exclusion_reason"] is None]
    excluded_events = [event for event in events if event["exclusion_reason"] is not None]
    if adjusted is None:
        comment = "Adjusted runway kan inte beräknas med tillgänglig kassa och burn."
    else:
        adjusted_label = f"{adjusted:g}".replace(".", ",")
        reported_part = "" if reported is None else f" Rapporten angav {reported:g} mån".replace(".", ",")
        elapsed = f"{months_since_report:.1f}".replace(".", ",")
        comment = (f"Runway från idag {adjusted_label} mån.{reported_part}"
                   f" vid periodslut, {elapsed} mån sedan.")
        if included_events:
            included_labels = ", ".join(
                f"{event.get('category_label') or event['category']} "
                f"{_amount_label(event['cash_effect'], event.get('currency'))} "
                f"vid {event['weight'] * 100:g} %".replace(".", ",")
                for event in included_events
            )
            comment += f" Inkluderat i pro forma: {included_labels}."
        else:
            comment += " Ingen post-report-finansiering påverkar pro forma."
    if excluded_events:
        excluded_labels = ", ".join(
            f"{event.get('category_label') or event['category']} ({event['exclusion_reason']})"
            for event in excluded_events
        )
        comment += f" Undantaget: {excluded_labels}."
    return {"issuer_id": assessment.get("issuer_id"), "reported_runway_months": reported,
            "adjusted_runway_months": adjusted,
            "delta_runway_months": round(adjustment / burn, 1) if has_burn else None,
            "runway_from_today_months": from_today, "months_since_report": round(months_since_report, 1),
            "cash": cash, "monthly_burn": burn, "currency": currency,
            "cash_flow_positive": positive, "runway_band": runway_band(adjusted, positive=positive),
            "report_period_end": period, "report_document_id": assessment.get("report_document_id"),
            "assessment_id": assessment.get("id"), "events": events,
            "pro_forma_comment": comment, "pro_forma_cash_adjustment": adjustment,
            "calculation_version": VERSION, "as_of": as_of,
            "basis": ("Rapporterad kassa framrullad med burn till idag; genomförd finansiering räknas, "
                      "annonserad viktas 0 tills vikt sätts. Bruttobelopp är före kostnader.")}


def current_analysis(connection: sqlite3.Connection, issuer_ids: list[int] | None = None,
                     preview: dict[str, float] | None = None, *,
                     transactions: list[dict] | None = None, check_cancelled=None) -> list[dict]:
    if transactions is None:
        if issuer_ids is not None:
            from .transaction_feed import _build_transaction_items
            transactions = _build_transaction_items(connection, issuer_ids=issuer_ids, check_cancelled=check_cancelled)
        else:
            transactions = _transaction_items(connection)
    selected_ids = {int(value) for value in issuer_ids} if issuer_ids is not None else None
    if selected_ids is not None:
        transactions = [tx for tx in transactions if tx.get("issuer_id") in selected_ids]
    # Safety facts stay local to this additive analysis; the canonical feed and
    # screening calculations retain their existing semantics.
    source_text = {}
    source_ids = sorted({i for tx in transactions for i in tx.get("source_document_ids", [])})
    for offset in range(0, len(source_ids), 500):
        if check_cancelled:
            check_cancelled()
        batch = source_ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        source_text.update({row["id"]: (row["title"] or "") + " " + (row["extracted_text"] or "")
                            for row in connection.execute(
                                "SELECT d.id,d.title,c.extracted_text FROM documents d LEFT JOIN document_contents c ON c.id=("
                                "SELECT c2.id FROM document_contents c2 WHERE c2.document_id=d.id "
                                "AND c2.status IN ('extracted','too_short') AND c2.extracted_text IS NOT NULL "
                                "ORDER BY (c2.attachment_id IS NULL) DESC,c2.quality_score DESC,c2.id DESC LIMIT 1) "
                                "WHERE d.id IN (" + placeholders + ")", batch)})
    transactions = [{**tx, "_source_text": " ".join(source_text.get(i, "") for i in tx.get("source_document_ids", []))}
                    for tx in transactions]
    assessment_sql = (
        "SELECT r.* FROM runway_assessments r WHERE r.id=(SELECT r2.id FROM runway_assessments r2 "
        "WHERE r2.issuer_id=r.issuer_id ORDER BY r2.report_period_end DESC,r2.id DESC LIMIT 1)"
    )
    assessment_args: tuple[int, ...] = ()
    if selected_ids is not None:
        if not selected_ids:
            rows = []
        else:
            assessment_sql += " AND r.issuer_id IN (" + ",".join("?" for _ in selected_ids) + ")"
            assessment_args = tuple(sorted(selected_ids))
            rows = connection.execute(assessment_sql, assessment_args).fetchall()
    else:
        rows = connection.execute(assessment_sql).fetchall()
    weights = {row["transaction_id"]: row["weight"] for row in connection.execute("SELECT * FROM radar_financing_weights ORDER BY id")}
    if preview:
        for key, value in preview.items():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Vikten måste vara 0–100 %")
            weights[key] = value
    today = datetime.now(timezone.utc).date().isoformat()
    result = []
    for row in rows:
        if check_cancelled:
            check_cancelled()
        result.append(calculate(dict(row), transactions, weights, as_of=today))
    return result


def save_weight(connection, transaction_id, weight, *, actor, reason):
    if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 1:
        raise ValueError("Vikten måste vara 0–100 %")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("Ange en ändringsorsak")
    tx = next((t for t in _transaction_items(connection) if t["id"] == transaction_id), None)
    if tx is None:
        raise KeyError(transaction_id)
    connection.execute("INSERT INTO radar_financing_weights(issuer_id,transaction_id,weight,actor,reason,created_at) VALUES(?,?,?,?,?,?)",
                       (tx["issuer_id"], transaction_id, weight, actor, reason.strip(), datetime.now(timezone.utc).isoformat()))
    return current_analysis(connection, [tx["issuer_id"]])


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _audit_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["correction_ids"] = json.loads(payload.pop("correction_ids_json"))
    payload["before"] = json.loads(payload.pop("before_json"))
    payload["after"] = json.loads(payload.pop("after_json"))
    payload["included_events"] = [
        event for event in payload["after"]["events"]
        if event.get("exclusion_reason") is None
    ]
    return payload


def unavailable_runway_snapshot(issuer_id: int) -> dict[str, Any]:
    """An explicit absence marker, never an invented assessment or zero runway."""
    return {'issuer_id': issuer_id, 'reported_runway_months': None,
            'adjusted_runway_months': None, 'calculation_status': 'unavailable',
            'unavailable_reason': 'runway_underlying_missing', 'events': []}


def recalculate_adjusted_runway(
    connection: sqlite3.Connection,
    issuer_id: int,
    *,
    idempotency_key: str,
    actor: str,
    reason: str,
    correction_ids: list[int] | tuple[int, ...] = (),
    before_snapshot: dict[str, Any] | None = None,
    check_cancelled=None,
) -> dict[str, Any]:
    """Audit one effective-view pro forma calculation without editing screening.

    Pass ``before_snapshot`` from the same transaction when a correction apply
    calls this function. A direct recalculate uses the preceding audit result
    as its before value, or the present view if none has been audited yet.
    """
    if not isinstance(issuer_id, int) or isinstance(issuer_id, bool) or issuer_id <= 0:
        raise ValueError("issuer_id måste vara ett positivt heltal")
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key.strip()) <= 200:
        raise ValueError("Idempotency-Key måste vara 8–200 tecken")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("Aktör saknas")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("Ange en ändringsorsak")
    if not isinstance(correction_ids, (list, tuple)) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in correction_ids
    ):
        raise ValueError("correction_ids måste innehålla positiva heltal")
    ids = sorted(set(correction_ids))
    request = {"issuer_id": issuer_id, "correction_ids": ids,
               "reason": reason.strip(), "actor": actor.strip()}
    request_hash = hashlib.sha256(_stable_json(request).encode("utf-8")).hexdigest()
    existing = connection.execute(
        "SELECT * FROM adjusted_runway_recalculations WHERE issuer_id=? AND idempotency_key=?",
        (issuer_id, idempotency_key.strip()),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise ValueError("Idempotency-Key har redan använts med annat innehåll")
        return _audit_payload(existing)

    result = current_analysis(connection, [issuer_id], check_cancelled=check_cancelled)
    if not result and not connection.execute('SELECT 1 FROM issuers WHERE id=?', (issuer_id,)).fetchone():
        raise KeyError(issuer_id)
    after = result[0] if result else unavailable_runway_snapshot(issuer_id)
    if before_snapshot is None:
        preceding = connection.execute(
            """SELECT after_json FROM adjusted_runway_recalculations
               WHERE issuer_id=? AND status='completed' ORDER BY id DESC LIMIT 1""",
            (issuer_id,),
        ).fetchone()
        before = json.loads(preceding["after_json"]) if preceding else after
    else:
        before = before_snapshot
    if before.get("issuer_id") != issuer_id:
        raise ValueError("Före-värdet tillhör ett annat bolag")
    if before.get("reported_runway_months") != after.get("reported_runway_months"):
        raise ValueError("Reported runway får inte ändras vid omräkning")
    event_ids = {
        int(value) for event in after.get("events", [])
        for value in event.get("correction_ids", [])
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    ids = sorted(set(ids) | event_ids)
    calculation_hash = hashlib.sha256(_stable_json(after).encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    cursor = connection.execute(
        """INSERT INTO adjusted_runway_recalculations
           (issuer_id,idempotency_key,request_hash,calculation_hash,
            correction_ids_json,before_json,after_json,actor,reason,status,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,'completed',?)""",
        (issuer_id, idempotency_key.strip(), request_hash, calculation_hash,
         _stable_json(ids), _stable_json(before), _stable_json(after),
         actor.strip(), reason.strip(), created_at),
    )
    return _audit_payload(connection.execute(
        "SELECT * FROM adjusted_runway_recalculations WHERE id=?", (cursor.lastrowid,)
    ).fetchone())


def capture_month_end(connection, *, now: datetime | None = None) -> int:
    """Capture only the actual month-end; no retrospective reconstruction.

    Called by the server's bounded observer, never by a GET endpoint.
    The first observed month-end value is immutable and includes its inputs.
    """
    observation = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    actual = datetime.now(timezone.utc)
    if observation.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") != actual.strftime("%Y-%m-%dT%H:%M"):
        raise ValueError("Snapshots require an actual current observation; backfilling is not supported")
    today = observation.date()
    if today.day != calendar.monthrange(today.year, today.month)[1] or (observation.hour, observation.minute) != (23, 59):
        return 0
    count = 0
    for item in current_analysis(connection):
        cursor = connection.execute("INSERT OR IGNORE INTO issuer_runway_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item["issuer_id"], today.isoformat(), item["reported_runway_months"], item["adjusted_runway_months"], item["runway_band"],
             item["cash"], item["monthly_burn"], VERSION, json.dumps(item, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat()))
        count += cursor.rowcount
    return count


class SnapshotObserver:
    """Observe the last UTC minute of a month; downtime remains a history gap."""
    def __init__(self, database):
        self.database = database
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="radar-snapshots")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stopped.is_set():
            now = datetime.now(timezone.utc)
            if now.day == calendar.monthrange(now.year, now.month)[1] and (now.hour, now.minute) == (23, 59):
                try:
                    with self.database.connect() as connection:
                        import time
                        deadline = time.monotonic() + 55
                        connection.set_progress_handler(lambda: int(time.monotonic() > deadline or self.stopped.is_set()), 10000)
                        capture_month_end(connection, now=now)
                except Exception:
                    logging.getLogger(__name__).exception("Month-end observation failed; history remains missing")
            self.stopped.wait(30)


def market_health(connection, *, today: date | None = None, transactions: list[dict] | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    end = today.year * 12 + today.month - 1
    months = []
    for index in range(end - 11, end + 1):
        year, month0 = divmod(index, 12)
        months.append(f"{year:04}-{month0 + 1:02}")
    deals = {m: 0 for m in months}
    for tx in _transaction_items(connection) if transactions is None else transactions:
        first = min((h.get("published_at", "") for h in tx.get("history", []) if h.get("published_at")), default=tx.get("published_at", ""))
        if first[:7] in deals:
            deals[first[:7]] += 1
    snapshots = connection.execute("SELECT snapshot_date, runway_band, COUNT(*) AS count FROM issuer_runway_snapshots GROUP BY snapshot_date, runway_band").fetchall()
    by_month = {}
    for row in snapshots:
        month = row["snapshot_date"][:7]
        by_month.setdefault(month, {})[row["runway_band"] or "unknown"] = row["count"]
    return {"months": [{"month": m, "deal_count": deals[m], "runway_bands": by_month.get(m),
                        "snapshot_available": m in by_month} for m in months],
            "calculation_version": VERSION, "history_basis": "Oföränderliga observationer på månadens sista dag. Saknade observationer visas som luckor."}
