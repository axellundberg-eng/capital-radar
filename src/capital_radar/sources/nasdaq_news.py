from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from ..http import HttpClient, HttpError
from ..models import AttachmentCandidate, DocumentCandidate
from ..normalization import stable_hash, stockholm_timezone_for_local


BASE_URL = "https://api.news.eu.nasdaq.com/news/query.action"
CALLBACK = "handleResponse"
TARGET_MARKETS = {"Main Market, Stockholm", "First North Sweden"}


@dataclass(frozen=True)
class NasdaqNewsResult:
    items: tuple[DocumentCandidate, ...]
    response_hash: str
    status: int
    urls: tuple[str, ...]


def build_url(*, start: int, limit: int) -> str:
    return (
        f"{BASE_URL}?"
        + urlencode(
            {
                "countResults": "true",
                "globalGroup": "exchangeNotice",
                "displayLanguage": "en",
                "timeZone": "CET",
                "dateMask": "yyyy-MM-dd HH:mm:ss",
                "limit": limit,
                "start": start,
                "dir": "DESC",
                "globalName": "NordicAllMarkets",
                "callback": CALLBACK,
            }
        )
    )


def parse_jsonp(body: bytes | str) -> dict:
    text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        raise HttpError("Nasdaq CNS returnerade ogiltig JSONP")
    try:
        payload = json.loads(text[first : last + 1])
    except json.JSONDecodeError as exc:
        raise HttpError("Nasdaq CNS JSONP kunde inte avkodas") from exc
    if not isinstance(payload, dict):
        raise HttpError("Nasdaq CNS returnerade oväntad rot")
    return payload


def parse_items(payload: dict) -> tuple[DocumentCandidate, ...]:
    rows = ((payload.get("results") or {}).get("item")) or []
    candidates: list[DocumentCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("market") not in TARGET_MARKETS:
            continue
        external_id = str(row.get("disclosureId") or "").strip()
        issuer_name = str(row.get("company") or "").strip()
        title = str(row.get("headline") or "").strip()
        url = str(row.get("messageUrl") or "").strip()
        published = str(row.get("published") or row.get("releaseTime") or "").strip()
        if not (external_id and issuer_name and title and url and published):
            continue
        local_naive = datetime.strptime(published, "%Y-%m-%d %H:%M:%S")
        local_time = local_naive.replace(
            tzinfo=stockholm_timezone_for_local(local_naive)
        )
        attachments = tuple(
            AttachmentCandidate(
                url=str(attachment.get("attachmentUrl") or ""),
                file_name=attachment.get("fileName"),
                mime_type=attachment.get("mimetype"),
            )
            for attachment in (row.get("attachment") or [])
            if attachment.get("attachmentUrl")
        )
        candidates.append(
            DocumentCandidate(
                source_id="nasdaq_cns",
                external_id=external_id,
                issuer_name=issuer_name,
                title=title,
                canonical_url=url,
                published_at=local_time.isoformat(),
                language=row.get("language"),
                category=row.get("cnsCategory"),
                attachments=attachments,
                metadata={
                    "market": row.get("market"),
                    "category_id": row.get("categoryId"),
                    "cns_type_id": row.get("cnsTypeId"),
                    "available_languages": row.get("languages") or [],
                    "discovery_mode": "official_exchange_jsonp",
                },
            )
        )
    return tuple(candidates)


class NasdaqNewsAdapter:
    source_id = "nasdaq_cns"
    source_url = BASE_URL

    def __init__(self, http: HttpClient, page_size: int = 100) -> None:
        self.http = http
        self.page_size = page_size

    def fetch(self, *, max_items: int = 250) -> NasdaqNewsResult:
        start = 0
        all_items: list[DocumentCandidate] = []
        raw_bodies: list[bytes] = []
        urls: list[str] = []
        status = 200
        raw_row_count = 0
        while start < max_items:
            size = min(self.page_size, max_items - start)
            url = build_url(start=start, limit=size)
            response = self.http.get(
                url, accept="application/javascript, application/json, */*"
            )
            status = response.status
            urls.append(url)
            raw_bodies.append(response.body)
            payload = parse_jsonp(response.body)
            rows = ((payload.get("results") or {}).get("item")) or []
            raw_row_count += len(rows)
            all_items.extend(parse_items(payload))
            if len(rows) < size:
                break
            start += size
        deduplicated = {item.external_id: item for item in all_items}
        if raw_row_count == 0:
            raise RuntimeError("Nasdaq CNS coverage guard: flödet gav inga poster")
        return NasdaqNewsResult(
            items=tuple(deduplicated.values()),
            response_hash=stable_hash(*raw_bodies),
            status=status,
            urls=tuple(urls),
        )
