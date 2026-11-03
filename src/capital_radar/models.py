from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecurityCandidate:
    source_id: str
    external_id: str
    issuer_name: str
    display_name: str
    symbol: str
    isin: str
    currency: str | None
    market: str
    segment: str
    mic: str | None = None
    jurisdiction_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentCandidate:
    url: str
    file_name: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class DocumentCandidate:
    source_id: str
    external_id: str
    issuer_name: str
    title: str
    canonical_url: str
    published_at: str
    language: str | None = None
    category: str | None = None
    attachments: tuple[AttachmentCandidate, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    body: bytes
    content_type: str | None
    fetched_at: str

