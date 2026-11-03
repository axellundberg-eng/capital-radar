"""Pydantic-scheman för granskarens svar.

Schemat är single source of truth: samma klass genererar JSON-schemat som
skickas till providern (strukturerad output) och validerar svaret efteråt.
Ett svar som inte validerar blir ``validation_failed`` — det repareras aldrig.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Verdict(str, Enum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    UNCERTAIN = "uncertain"


class IssueCode(str, Enum):
    """Vad granskaren anser är fel. Tom lista vid ``confirmed``."""

    LIQUIDITY_EFFECT_KIND = "liquidity_effect_kind"
    REFLECTED_IN_REPORT = "reflected_in_report"
    AMOUNT = "amount"
    UNIT_SCALE = "unit_scale"
    ANNOUNCED_VS_COMPLETED = "announced_vs_completed"
    GROSS_VS_NET = "gross_vs_net"
    LOAN_COUNTED_AS_CASH = "loan_counted_as_cash"
    CASH_DIRECTION = "cash_direction"
    PERIOD_OR_DATE = "period_or_date"
    FACT_TYPE = "fact_type"
    QUOTE_DOES_NOT_SUPPORT = "quote_does_not_support"
    INSUFFICIENT_SOURCE = "insufficient_source"
    OTHER = "other"


class ItemReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["financial_fact", "financing_event"]
    target_id: int = Field(description="id från indata (fact_id eller event_id)")
    verdict: Verdict
    issues: list[IssueCode] = Field(
        default_factory=list,
        description="Tom vid confirmed. Annars de kontroller som inte håller.",
    )
    reasoning: str = Field(description="Kort motivering på svenska.")
    source_quote: str = Field(
        description="Ordagrant citat ur källtexten som utlåtandet vilar på; "
        "tom sträng om inget stöd finns.",
    )
    expected_value: Optional[str] = Field(
        default=None,
        description="Vid disputed: vad källan faktiskt säger, t.ex. "
        "'12,4 MSEK netto' eller 'committed_liquidity'. Annars null.",
    )


class ExtractionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemReview]
    overall_comment: str = Field(description="En till två meningar på svenska.")


class HeadlineFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int
    may_affect_cash: bool
    reasoning: str = Field(description="Kort motivering på svenska.")


class HeadlineBatchReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[HeadlineFlag]
