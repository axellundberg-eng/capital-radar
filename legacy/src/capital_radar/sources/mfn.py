from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..http import HttpClient
from ..models import AttachmentCandidate, DocumentCandidate
from ..normalization import stable_hash, stockholm_timezone_for_local


BASE_URL = "https://www.mfn.se"
FEED_URL = f"{BASE_URL}/a/"


class MfnIssuerPageEmptyError(RuntimeError):
    """The requested MFN author page exists but contains no parseable items."""


@dataclass(frozen=True)
class MfnDiscoveryResult:
    items: tuple[DocumentCandidate, ...]
    response_hash: str
    status: int
    url: str


class _MfnFeedParser(HTMLParser):
    def __init__(
        self,
        *,
        default_issuer_name: str | None = None,
        default_issuer_slug: str | None = None,
        default_issuer_url: str | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.current: dict | None = None
        self.capture: str | None = None
        self.default_issuer_name = default_issuer_name
        self.default_issuer_slug = default_issuer_slug
        self.default_issuer_url = default_issuer_url

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = self._attrs(attrs)
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "short-item" in classes:
            self._flush()
            self.current = {
                "external_id": attributes.get("id", ""),
                "attachments": [],
                "issuer_name": self.default_issuer_name or "",
                "issuer_slug": self.default_issuer_slug or "",
                "issuer_href": self.default_issuer_url or "",
            }
            return
        if self.current is None:
            return
        if tag == "span":
            if "compressed-date" in classes:
                self.capture = "date"
            elif "compressed-time" in classes:
                self.capture = "time"
        elif tag == "a":
            if "author-link" in classes:
                self.current["issuer_href"] = attributes.get("href", "")
                self.current["issuer_slug"] = attributes.get("author", "")
                self.capture = "issuer_name"
            elif "item-link" in classes:
                self.current["canonical_url"] = attributes.get("href", "")
                self.current["title"] = attributes.get("title", "")
                self.capture = "title_text"
            elif "attachment-icon" in classes:
                url = attributes.get("href", "")
                if url:
                    self.current["attachments"].append(
                        {
                            "url": url,
                            "file_name": urlsplit(url).path.rsplit("/", 1)[-1] or None,
                            "mime_type": (
                                "application/pdf"
                                if urlsplit(url).path.casefold().endswith(".pdf")
                                else None
                            ),
                        }
                    )

    def handle_endtag(self, tag: str) -> None:
        if tag in {"span", "a"}:
            self.capture = None

    def handle_data(self, data: str) -> None:
        if self.current is None or self.capture is None:
            return
        value = data.strip()
        if not value:
            return
        if self.capture == "title_text" and self.current.get("title"):
            return
        self.current[self.capture] = (
            f"{self.current.get(self.capture, '')} {value}".strip()
        )

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self.current and all(
            self.current.get(key)
            for key in (
                "external_id",
                "date",
                "time",
                "issuer_name",
                "title",
                "canonical_url",
            )
        ):
            self.items.append(self.current)
        self.current = None
        self.capture = None


def parse_feed(
    html: str,
    *,
    limit: int | None = None,
    default_issuer_name: str | None = None,
    default_issuer_slug: str | None = None,
    default_issuer_url: str | None = None,
) -> tuple[DocumentCandidate, ...]:
    parser = _MfnFeedParser(
        default_issuer_name=default_issuer_name,
        default_issuer_slug=default_issuer_slug,
        default_issuer_url=default_issuer_url,
    )
    parser.feed(html)
    parser.close()
    candidates: list[DocumentCandidate] = []
    for row in parser.items[:limit]:
        local_naive = datetime.strptime(
            f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M:%S"
        )
        local_time = local_naive.replace(
            tzinfo=stockholm_timezone_for_local(local_naive)
        )
        attachments = tuple(
            AttachmentCandidate(**attachment)
            for attachment in row.get("attachments", [])
        )
        candidates.append(
            DocumentCandidate(
                source_id="mfn",
                external_id=row["external_id"],
                issuer_name=row["issuer_name"],
                title=row["title"],
                canonical_url=urljoin(BASE_URL, row["canonical_url"]),
                published_at=local_time.isoformat(),
                attachments=attachments,
                metadata={
                    "issuer_slug": row.get("issuer_slug"),
                    "issuer_url": urljoin(BASE_URL, row.get("issuer_href", "")),
                    "discovery_mode": "feed_metadata_only",
                },
            )
        )
    return tuple(candidates)


class MfnDiscoveryAdapter:
    source_id = "mfn"
    source_url = FEED_URL

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, *, limit: int | None = None) -> MfnDiscoveryResult:
        response = self.http.get(FEED_URL, accept="text/html,application/xhtml+xml")
        html = response.body.decode("utf-8", errors="replace")
        items = parse_feed(html, limit=limit)
        if not items:
            raise RuntimeError("MFN coverage guard: flödet gav inga tolkbara poster")
        return MfnDiscoveryResult(
            items=items,
            response_hash=stable_hash(response.body),
            status=response.status,
            url=response.url,
        )

    def fetch_issuer(
        self,
        issuer_url: str,
        *,
        issuer_name: str,
        issuer_slug: str,
        limit: int = 50,
    ) -> MfnDiscoveryResult:
        expected_url = f"{BASE_URL}/all/a/{issuer_slug}"
        if issuer_url.rstrip("/") != expected_url:
            raise ValueError("MFN-bolagslänken matchar inte den validerade sluggen")
        response = self.http.get(
            issuer_url,
            accept="text/html,application/xhtml+xml",
        )
        html = response.body.decode("utf-8", errors="replace")
        items = parse_feed(
            html,
            limit=limit,
            default_issuer_name=issuer_name,
            default_issuer_slug=issuer_slug,
            default_issuer_url=issuer_url,
        )
        if not items:
            raise MfnIssuerPageEmptyError(
                "MFN-bolagssidan gav inga tolkbara pressmeddelanden"
            )
        return MfnDiscoveryResult(
            items=items,
            response_hash=stable_hash(response.body),
            status=response.status,
            url=response.url,
        )
