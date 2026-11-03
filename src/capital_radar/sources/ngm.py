from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from ..http import HttpClient
from ..models import SecurityCandidate
from ..normalization import isin_jurisdiction, stable_hash


BASE_URL = "https://ngm-api-prod.vmate.se/instrument/list"


@dataclass(frozen=True)
class NgmUniverseResult:
    items: tuple[SecurityCandidate, ...]
    response_hash: str
    status: int
    urls: tuple[str, ...]


def build_url(page: int, size: int = 100) -> str:
    return (
        f"{BASE_URL}?"
        + urlencode(
            {
                "page": page,
                "size": size,
                "market": "equities",
                "instrumentType": "ALL",
                "marketSegments": "NGM Growth Market",
                "listCountry": "SE",
            }
        )
    )


def parse_rows(payload: dict) -> tuple[SecurityCandidate, ...]:
    rows = payload.get("data") or []
    items: list[SecurityCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("marketSegment") != "NGM Growth Market":
            continue
        row_type = str(row.get("type") or "").casefold()
        if (
            row_type
            and "equity" not in row_type
            and "share" not in row_type
            and "stock" not in row_type
        ):
            continue
        isin = str(row.get("isin") or "").strip().upper()
        external_id = str(row.get("insref") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        display_name = str(row.get("name") or "").strip()
        # NGM's `issuer` field is often an old/short ticker (for example ORE
        # for Eurobattery Minerals). The instrument name is the more reliable
        # public company label for entity matching.
        issuer_name = display_name
        if not (isin and external_id and symbol and display_name and issuer_name):
            continue
        items.append(
            SecurityCandidate(
                source_id="ngm",
                external_id=external_id,
                issuer_name=issuer_name,
                display_name=display_name,
                symbol=symbol,
                isin=isin,
                currency=None,
                market="NGM Growth Market",
                segment="NGM Growth Market",
                mic="NSME",
                jurisdiction_code=isin_jurisdiction(isin),
                metadata={
                    "instrument_reference": external_id,
                    "instrument_type": row.get("type"),
                    "raw_issuer_field": row.get("issuer"),
                    "source_market_segment": row.get("marketSegment"),
                    "country_based_on_parent_list": row.get(
                        "country_based_on_grandparent_list"
                    ),
                },
            )
        )
    return tuple(items)


class NgmUniverseAdapter:
    source_id = "ngm"
    source_url = BASE_URL

    def __init__(self, http: HttpClient, page_size: int = 100) -> None:
        self.http = http
        self.page_size = page_size

    def fetch(self) -> NgmUniverseResult:
        page = 0
        total = None
        all_items: list[SecurityCandidate] = []
        raw_bodies: list[bytes] = []
        urls: list[str] = []
        status = 200
        while total is None or page * self.page_size < total:
            url = build_url(page, self.page_size)
            payload, response = self.http.get_json(url)
            status = response.status
            urls.append(url)
            raw_bodies.append(response.body)
            total = int(payload.get("totalNumber") or 0)
            parsed = parse_rows(payload)
            all_items.extend(parsed)
            if not payload.get("data") or total == 0:
                break
            page += 1
        deduplicated = {item.external_id: item for item in all_items}
        if len(deduplicated) < 50:
            raise RuntimeError(
                "NGM coverage guard: "
                f"Growth Market gav {len(deduplicated)} aktier (minimum 50)"
            )
        return NgmUniverseResult(
            items=tuple(deduplicated.values()),
            response_hash=stable_hash(*raw_bodies),
            status=status,
            urls=tuple(urls),
        )
