from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from ..http import HttpClient
from ..models import SecurityCandidate
from ..normalization import isin_jurisdiction, stable_hash


BASE_URL = "https://api.nasdaq.com/api/nordic/screener/shares"


@dataclass(frozen=True)
class NasdaqUniverseResult:
    items: tuple[SecurityCandidate, ...]
    response_hash: str
    status: int
    urls: tuple[str, ...]


SEGMENTS = (
    {
        "segment_code": "SMALL_CAP",
        "segment_label": "Small Cap",
        "category": "MAIN_MARKET",
        "market": "Nasdaq Stockholm",
        "mic": "XSTO",
    },
    {
        "segment_code": "FN_GM",
        "segment_label": "First North GM",
        "category": "FIRST_NORTH",
        "market": "Nasdaq First North Growth Market Sweden",
        "mic": "FNSE",
    },
    {
        "segment_code": "FN_PREMIER",
        "segment_label": "First North Premier GM",
        "category": "FIRST_NORTH",
        "market": "Nasdaq First North Growth Market Sweden",
        "mic": "FNSE",
    },
)

MINIMUM_SEGMENT_COUNTS = {
    "SMALL_CAP": 50,
    "FN_GM": 150,
    "FN_PREMIER": 20,
}


def build_url(segment: dict[str, str]) -> str:
    return (
        f"{BASE_URL}?"
        + urlencode(
            {
                "category": segment["category"],
                "market": "STO",
                "segment": segment["segment_code"],
                "tableonly": "true",
            }
        )
    )


def parse_rows(
    payload: dict, *, segment: dict[str, str]
) -> tuple[SecurityCandidate, ...]:
    data = payload.get("data") or {}
    listing = data.get("instrumentListing") or {}
    rows = listing.get("rows") or []
    items: list[SecurityCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        isin = str(row.get("isin") or "").strip().upper()
        external_id = str(row.get("orderbookId") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        full_name = str(row.get("fullName") or "").strip()
        if not (isin and external_id and symbol and full_name):
            continue
        items.append(
            SecurityCandidate(
                source_id="nasdaq_nordic",
                external_id=external_id,
                issuer_name=full_name,
                display_name=full_name,
                symbol=symbol,
                isin=isin,
                currency=(str(row.get("currency")).strip() or None),
                market=segment["market"],
                segment=segment["segment_label"],
                mic=segment["mic"],
                jurisdiction_code=isin_jurisdiction(isin),
                metadata={
                    "asset_class": row.get("assetClass"),
                    "sector": row.get("sector"),
                    "orderbook_id": external_id,
                    "source_segment_code": segment["segment_code"],
                },
            )
        )
    return tuple(items)


class NasdaqUniverseAdapter:
    source_id = "nasdaq_nordic"
    source_url = BASE_URL

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self) -> NasdaqUniverseResult:
        all_items: list[SecurityCandidate] = []
        raw_bodies: list[bytes] = []
        urls: list[str] = []
        status = 200
        seen_external_ids: set[str] = set()
        for segment in SEGMENTS:
            url = build_url(segment)
            payload, response = self.http.get_json(url)
            status = response.status
            urls.append(url)
            raw_bodies.append(response.body)
            api_status = payload.get("status") or {}
            if api_status.get("rCode") not in (None, 200):
                raise RuntimeError(
                    f"Nasdaq returnerade rCode={api_status.get('rCode')}"
                )
            parsed = parse_rows(payload, segment=segment)
            minimum = MINIMUM_SEGMENT_COUNTS[segment["segment_code"]]
            if len(parsed) < minimum:
                raise RuntimeError(
                    "Nasdaq coverage guard: "
                    f"{segment['segment_code']} gav {len(parsed)} poster "
                    f"(minimum {minimum})"
                )
            for item in parsed:
                if item.external_id not in seen_external_ids:
                    seen_external_ids.add(item.external_id)
                    all_items.append(item)
        return NasdaqUniverseResult(
            items=tuple(all_items),
            response_hash=stable_hash(*raw_bodies),
            status=status,
            urls=tuple(urls),
        )
