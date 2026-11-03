from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree

from ..http import HttpClient
from ..models import AttachmentCandidate, DocumentCandidate
from ..normalization import stable_hash


@dataclass(frozen=True)
class IssuerFeed:
    issuer_name: str
    url: str
    language: str | None = None


@dataclass(frozen=True)
class IssuerFeedResult:
    items: tuple[DocumentCandidate, ...]
    response_hash: str
    status: int
    urls: tuple[str, ...]


def load_feeds(path: Path | str) -> tuple[IssuerFeed, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("issuer_feeds.json måste innehålla en lista")
    feeds: list[IssuerFeed] = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("issuer_name") or not row.get("url"):
            raise ValueError("Varje IR-flöde måste ha issuer_name och url")
        feeds.append(
            IssuerFeed(
                issuer_name=str(row["issuer_name"]),
                url=str(row["url"]),
                language=(str(row["language"]) if row.get("language") else None),
            )
        )
    return tuple(feeds)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(node: ElementTree.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _published_at(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def parse_feed(
    body: bytes, *, feed: IssuerFeed
) -> tuple[DocumentCandidate, ...]:
    root = ElementTree.fromstring(body)
    nodes = [
        node
        for node in root.iter()
        if _local_name(node.tag) in {"item", "entry"}
    ]
    candidates: list[DocumentCandidate] = []
    for node in nodes:
        title = _child_text(node, "title")
        published = _child_text(node, "pubDate", "published", "updated", "date")
        external_id = _child_text(node, "guid", "id")
        category = _child_text(node, "category") or None
        url = _child_text(node, "link")
        attachments: list[AttachmentCandidate] = []
        for child in node:
            name = _local_name(child.tag)
            if name == "link":
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "")
                if href and rel == "enclosure":
                    attachments.append(
                        AttachmentCandidate(
                            url=urljoin(feed.url, href),
                            mime_type=child.attrib.get("type"),
                        )
                    )
                elif href and not url:
                    url = href
            elif name == "enclosure" and child.attrib.get("url"):
                attachments.append(
                    AttachmentCandidate(
                        url=urljoin(feed.url, child.attrib["url"]),
                        mime_type=child.attrib.get("type"),
                    )
                )
        url = urljoin(feed.url, url)
        if not external_id:
            external_id = stable_hash(feed.url, url, title, published)
        if not (title and published and url):
            continue
        candidates.append(
            DocumentCandidate(
                source_id="issuer_ir",
                external_id=external_id,
                issuer_name=feed.issuer_name,
                title=title,
                canonical_url=url,
                published_at=_published_at(published),
                language=feed.language,
                category=category,
                attachments=tuple(attachments),
                metadata={
                    "feed_url": feed.url,
                    "discovery_mode": "issuer_original_rss_atom",
                },
            )
        )
    return tuple(candidates)


class IssuerFeedAdapter:
    source_id = "issuer_ir"
    source_url = "configured-per-issuer"

    def __init__(self, http: HttpClient, feeds: tuple[IssuerFeed, ...]) -> None:
        self.http = http
        self.feeds = feeds

    def fetch(self) -> IssuerFeedResult:
        all_items: list[DocumentCandidate] = []
        bodies: list[bytes] = []
        urls: list[str] = []
        status = 204 if not self.feeds else 200
        for feed in self.feeds:
            response = self.http.get(
                feed.url,
                accept=(
                    "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml, */*"
                ),
            )
            status = response.status
            urls.append(feed.url)
            bodies.append(response.body)
            all_items.extend(parse_feed(response.body, feed=feed))
        deduplicated = {item.external_id: item for item in all_items}
        return IssuerFeedResult(
            items=tuple(deduplicated.values()),
            response_hash=stable_hash(*bodies),
            status=status,
            urls=tuple(urls),
        )

