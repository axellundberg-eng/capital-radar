"""Canonical Pydantic-modeller för finansiell extraktion (V2).

Detta är SINGLE SOURCE OF TRUTH för extraktions-schemat i den nya
AI-runtime:n (openai + instructor + pydantic). All strukturell validering
sker genom dessa modeller — ingen manuell schema-validering, ingen
frozenset-kontroll, ingen egen JSON-repair.

Modellerna speglar det kanoniska EXTRACTION_SCHEMA men UTAN ``bergs_fit``
(den flyttas downstream — extraktion ska bara producera strukturerade fakta,
inte en exekutiv prioriteringsscore). ``extra="ignore"`` gör att modellen
tyst ignorerar hallucinerade extrafält från providern istället för att krascha
hela extraktionen (med max_retries=0 hade ``extra="forbid"`` gjort att varje
sådant svar failade direkt och stoppade hela analys-pipelinen).

Legacy-artifacts (med bergs_fit) förblir läsbara; nya artifacts saknar det.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FactType(str, Enum):
    CASH_AND_CASH_EQUIVALENTS = "cash_and_cash_equivalents"
    SHORT_TERM_INVESTMENTS = "short_term_investments"
    RESTRICTED_CASH = "restricted_cash"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    INVESTING_CASH_FLOW = "investing_cash_flow"
    FINANCING_CASH_FLOW = "financing_cash_flow"
    CAPEX = "capex"
    INTEREST_BEARING_DEBT = "interest_bearing_debt"
    DEBT_DUE_WITHIN_12_MONTHS = "debt_due_within_12_months"
    UNDRAWN_FACILITIES = "undrawn_facilities"
    NET_DEBT = "net_debt"


class EventType(str, Enum):
    RIGHTS_ISSUE = "rights_issue"
    DIRECTED_ISSUE = "directed_issue"
    CONVERTIBLE = "convertible"
    WARRANT_EXERCISE = "warrant_exercise"
    LOAN_DRAW = "loan_draw"
    LOAN_COMMITMENT = "loan_commitment"
    CREDIT_FACILITY = "credit_facility"
    BRIDGE_LOAN = "bridge_loan"
    GRANT = "grant"
    ACQUISITION = "acquisition"
    DIVESTMENT = "divestment"
    DIVIDEND = "dividend"
    DEBT_REPAYMENT = "debt_repayment"
    OTHER = "other"


class EvidenceLabel(str, Enum):
    REPORTED_FACT = "reported_fact"
    MANAGEMENT_STATEMENT = "management_statement"
    UNKNOWN = "unknown"


class EventStatus(str, Enum):
    ANNOUNCED = "announced"
    APPROVED = "approved"
    CONDITIONAL = "conditional"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class CashDirection(str, Enum):
    INFLOW = "inflow"
    OUTFLOW = "outflow"
    NONE = "none"


class LiquidityEffectKind(str, Enum):
    CONFIRMED_CASH = "confirmed_cash"
    COMMITTED_LIQUIDITY = "committed_liquidity"
    CONDITIONAL_LIQUIDITY = "conditional_liquidity"
    NONE = "none"


class ReflectedInReport(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ReportedScale(str, Enum):
    ONES = "ones"
    THOUSANDS = "thousands"
    MILLIONS = "millions"


class DocumentClass(str, Enum):
    FINANCIAL_REPORT = "financial_report"
    FINANCING_ANNOUNCEMENT = "financing_announcement"
    TRANSACTION_ANNOUNCEMENT = "transaction_announcement"
    OTHER = "other"


class FinancialFact(BaseModel):
    """Ett strukturerat finansiellt faktum med provenans."""

    model_config = ConfigDict(extra="ignore")

    fact_type: FactType
    reported_value: Optional[float] = None
    normalized_value: Optional[float] = None
    currency: Optional[str] = None
    reported_unit: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    as_of_date: Optional[str] = None
    evidence_label: EvidenceLabel = EvidenceLabel.UNKNOWN
    source_page: Optional[int] = None
    source_excerpt: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class GuaranteeTranche(BaseModel):
    """A disclosed top, middle or bottom guarantee layer.

    This is an extraction contract, not a calculation request.  Leave every
    undisclosed amount null and never split a total guarantee across tiers.
    """

    model_config = ConfigDict(extra="ignore")

    tier: str = Field(
        description="Exactly top, middle, bottom or other; use other only when the source names no tier.",
    )
    commitment_amount: Optional[float] = Field(
        default=None, description="Explicit guaranteed amount in SEK; never inferred."
    )
    fee_rate: Optional[float] = Field(
        default=None, description="Explicit fee as decimal, e.g. 0.12 for 12 percent."
    )
    fee_amount: Optional[float] = Field(
        default=None, description="Explicit fee amount in SEK; never derived from rate times amount."
    )
    utilized_amount: Optional[float] = Field(
        default=None, description="Amount actually taken up by this layer at outcome, if stated."
    )
    source_page: Optional[int] = None
    source_excerpt: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class FinancingEvent(BaseModel):
    """En strukturerad finansieringshändelse med provenans."""

    model_config = ConfigDict(extra="ignore")

    event_type: EventType
    description: str = ""
    announcement_date: Optional[str] = None
    effective_date: Optional[str] = None
    status: EventStatus = Field(
        default=EventStatus.UNKNOWN,
        description=(
            "completed när finansieringen genomförts/betalats/tecknats. Ett "
            "emissionsutfall (övertecknad, övertilldelningsoption utnyttjas, "
            "'bolaget tillförs cirka X MSEK') är completed. Bara beslutad/"
            "beviljad/garanterad finansiering är announced/approved/conditional."
        ),
    )
    cash_direction: CashDirection = CashDirection.NONE
    gross_amount: Optional[float] = Field(
        default=None,
        description="Bruttobelopp som tillförs bolaget, om det anges.",
    )
    net_amount: Optional[float] = Field(
        default=None,
        description=(
            "Nettobelopp efter emissionkostnader. Lämnas null om det inte "
            "anges explicit — konvertera ALDRIG brutto till netto."
        ),
    )
    cash_effect_amount: Optional[float] = Field(
        default=None,
        description=(
            "Det belopp som faktiskt påverkar kassan. Vid ett genomfört "
            "emissionsutfall: sätt till det angivna bruttobeloppet om netto "
            "saknas (med lägre confidence). Null BARA om inget belopp alls anges."
        ),
    )
    currency: Optional[str] = None
    liquidity_effect_kind: LiquidityEffectKind = Field(
        default=LiquidityEffectKind.NONE,
        description=(
            "confirmed_cash när status=completed och cash_effect_amount är "
            "satt. committed_liquidity för beviljad men ej utnyttjad "
            "facilitet. conditional_liquidity för villkorad/beslutsfas."
        ),
    )
    committed_amount: Optional[float] = None
    conditional_amount: Optional[float] = None
    credit_limit: Optional[float] = Field(
        default=None,
        description="Avtalad kreditram. Är inte kassainflöde förrän ett uttag verifierats.",
    )
    drawn_amount: Optional[float] = Field(
        default=None,
        description="Källbelagt hittills utbetalt/utnyttjat kreditbelopp.",
    )
    undrawn_commitment: Optional[float] = Field(
        default=None,
        description="Kvarvarande outnyttjad kreditram när den uttryckligen anges.",
    )
    reflected_in_report: ReflectedInReport = Field(
        default=ReflectedInReport.UNKNOWN,
        description=(
            "no när händelsen inträffade EFTER rapportperiodens slut (t.ex. "
            "ett emissionsutfall publicerat efter balansdagen)."
        ),
    )
    conditions: Optional[str] = None
    use_of_proceeds: Optional[str] = None
    canonical_round_key: Optional[str] = Field(
        default=None,
        description="Stable source-backed identifier shared only by releases that explicitly concern the same financing round.",
    )
    lifecycle_stage: Optional[str] = Field(
        default=None,
        description="One of considering, announced, terms_published, subscription_open, preliminary_outcome, outcome_published, registered, settled, cancelled. Null when the document does not establish a stage.",
    )
    stage_effective_at: Optional[str] = None
    issue_price: Optional[float] = None
    subscription_commitment_amount: Optional[float] = None
    subscription_commitment_pct: Optional[float] = None
    guarantee_commitment_amount: Optional[float] = None
    guarantee_commitment_pct: Optional[float] = None
    guarantee_utilized_amount: Optional[float] = None
    guarantee_utilized_pct: Optional[float] = None
    guarantee_fee_details: Optional[str] = Field(
        default=None,
        description="Source-backed guarantee compensation, including any top, middle or bottom tier. Never estimate.",
    )
    guarantee_fee_amount: Optional[float] = Field(
        default=None,
        description="Explicit total guarantee compensation amount. Never derive it from the guaranteed amount.",
    )
    guarantee_fee_pct: Optional[float] = Field(
        default=None,
        description="Explicit guarantee fee rate as a decimal fraction, e.g. 0.12 for 12 percent. Null for tiered fees without one total rate.",
    )
    guarantee_fee_cash_pct: Optional[float] = Field(
        default=None,
        description="Kontant garantiersättning som decimalandel av angiven avgiftsbas.",
    )
    guarantee_fee_share_pct: Optional[float] = Field(
        default=None,
        description="Aktiebaserad garantiersättning som decimalandel; aldrig kontant utflöde.",
    )
    guarantee_fee_basis: Optional[str] = Field(
        default=None,
        description="Källangiven avgiftsbas, normalt guaranteed_amount. Gissa aldrig.",
    )
    guarantee_tranches: list[GuaranteeTranche] = Field(
        default_factory=list,
        description=(
            "Source-backed guarantee layers. Use only where a source explicitly "
            "identifies a top, middle, bottom or untiered guarantee and its terms."
        ),
    )
    transaction_cost: Optional[float] = Field(
        default=None,
        description="Explicit aggregate issue costs paid by the issuer. Null when costs are not stated.",
    )
    adviser: Optional[str] = None
    issuer_agent: Optional[str] = None
    outcome: Optional[str] = Field(
        default=None,
        description="Source-backed outcome only, e.g. subscription rate, guarantor take-up or settlement wording.",
    )
    evidence_label: EvidenceLabel = EvidenceLabel.UNKNOWN
    source_page: Optional[int] = None
    source_excerpt: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class FinancialReport(BaseModel):
    """Rapportnivå-metadata och extraherade fakta."""

    model_config = ConfigDict(extra="ignore")

    is_financial_report: bool = False
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    publication_date: Optional[str] = None
    currency: Optional[str] = None
    reported_scale: Optional[ReportedScale] = None
    period_months: Optional[float] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    facts: list[FinancialFact] = Field(default_factory=list)


class ExtractionQuality(BaseModel):
    """Kvalitetsflaggor för extraktionen."""

    model_config = ConfigDict(extra="ignore")

    complete: bool = False
    uncertainties: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


class CapitalNeedExtraction(BaseModel):
    """Canonical output för kapitalbehovsextraktion.

    Detta är rotmodellen som Instructor returnerar. Den innehåller INGET
    ``bergs_fit`` — det beräknas downstream av separata reasoning-agenter.
    """

    model_config = ConfigDict(extra="ignore")

    document_class: DocumentClass = DocumentClass.OTHER
    document_summary: str = ""
    report: FinancialReport = Field(default_factory=FinancialReport)
    financing_events: list[FinancingEvent] = Field(default_factory=list)
    quality: ExtractionQuality = Field(default_factory=ExtractionQuality)


__all__ = [
    "CapitalNeedExtraction",
    "CashDirection",
    "DocumentClass",
    "EventStatus",
    "EventType",
    "EvidenceLabel",
    "ExtractionQuality",
    "FactType",
    "FinancialFact",
    "FinancialReport",
    "FinancingEvent",
    "GuaranteeTranche",
    "LiquidityEffectKind",
    "ReflectedInReport",
    "ReportedScale",
]
