from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ScreeningEligibility:
    eligible: bool
    code: str
    reason: str
    priority: int


_COMPLEX_SECTOR_PATTERNS = (
    "real estate",
    "financial",
    "bank",
    "insurance",
)

_COMPLEX_NAME_PATTERNS = (
    r"\bfastighet",
    r"\bproperty\b",
    r"\breal estate\b",
    r"\brealty\b",
    r"\binvestment\b",
    r"\binvest\b",
    r"\binvestor\b",
    r"\bequity\b",
    r"\bventures?\b",
    r"\bacquisition\b",
    r"\bspac\b",
    r"\bbank\b",
    r"\bfinans\b",
    r"\bfinance\b",
    r"\bsecurities\b",
    r"\basset management\b",
    r"\bfonder?\b",
    r"\bfunds?\b",
    r"\binsurance\b",
    r"\bforsakring",
    r"\bkredit\b",
)

_PRIORITY_SECTORS = {
    "health care": 10,
    "technology": 25,
    "telecommunications": 30,
    "industrials": 40,
}


def classify_screening_eligibility(
    *,
    issuer_name: str,
    jurisdiction_code: str | None,
    sectors: tuple[str, ...] = (),
    override: bool | None = None,
    override_reason: str | None = None,
) -> ScreeningEligibility:
    if override is not None:
        return ScreeningEligibility(
            eligible=override,
            code="manual_include" if override else "manual_exclude",
            reason=override_reason
            or (
                "Manuellt inkluderad i batchscreening"
                if override
                else "Manuellt exkluderad från batchscreening"
            ),
            priority=0 if override else 999,
        )

    normalized_name = _normalize(issuer_name)
    normalized_sectors = tuple(
        _normalize(sector) for sector in sectors if sector
    )
    for sector in normalized_sectors:
        if any(pattern in sector for pattern in _COMPLEX_SECTOR_PATTERNS):
            return ScreeningEligibility(
                False,
                "complex_financial_model",
                (
                    f"Ej relevant för standard-runway: komplex "
                    f"balansräkningsdriven sektor ({sector})"
                ),
                999,
            )

    if any(
        re.search(pattern, normalized_name, re.IGNORECASE)
        for pattern in _COMPLEX_NAME_PATTERNS
    ):
        return ScreeningEligibility(
            False,
            "complex_company_type",
            (
                "Ej relevant för standard-runway: fastighets-, investment-, "
                "holding- eller finansbolag"
            ),
            999,
        )

    is_health_care = any(
        "health care" in sector for sector in normalized_sectors
    )
    jurisdiction = (jurisdiction_code or "").strip().upper()
    if jurisdiction and jurisdiction != "SE" and not is_health_care:
        return ScreeningEligibility(
            False,
            "foreign_non_medtech",
            (
                f"Utländsk emittent ({jurisdiction}) utanför prioriterad "
                "Health Care/Medtech-screening"
            ),
            999,
        )

    priority = 60
    for sector_name, sector_priority in _PRIORITY_SECTORS.items():
        if any(sector_name in sector for sector in normalized_sectors):
            priority = min(priority, sector_priority)
    reason = (
        "Prioriterad operativ Health Care/Medtech-emittent"
        if is_health_care
        else "Operativ emittent lämpad för standardiserad runway-screening"
    )
    return ScreeningEligibility(True, "eligible_operating_company", reason, priority)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.casefold()).strip()
