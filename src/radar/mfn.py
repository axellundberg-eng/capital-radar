"""MFN: hämta och tolka ett bolags nyhetssida, https://www.mfn.se/all/a/<slug>.

Sidan är serverrenderad HTML (ingen JavaScript behövs). Varje post är en
`div.short-item` med datum (`span.compressed-date`), tid (`span.compressed-time`),
rubriklänk (`a.item-link`, href `/a/<slug>/<post>`) och noll eller flera
bilagor (`a.attachment-icon`, PDF på storage.mfn.se). Tiderna är svensk lokal tid.
"""

from __future__ import annotations

import re
import threading
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .fetch import FetchError, allowed
from .settings import Settings

BASE_URL = "https://www.mfn.se"
MIN_INTERVAL = 0.75
MAX_PAGE_BYTES = 3_000_000
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-?")
SWEDISH = re.compile(r"[åäö]|\b(?:och|för|av|till|som|har|med|från|om|ett|är|inför|rapport|bolag)\b", re.I)
ENGLISH = re.compile(r"\b(?:the|and|of|to|for|in|has|is|with|from|announces|report|company|signs)\b", re.I)
CATEGORY_CLASSES = {"category", "tag", "item-category"}


@dataclass(frozen=True)
class MfnItem:
    title: str
    published_at: str            # ISO med tidszon, t.ex. 2026-10-23T07:40:00+02:00
    url: str                     # postens sida på MFN
    language: str                # sv eller en
    category: str | None = None  # MFN:s egen kategori om sidan anger en
    attachments: tuple[str, ...] = ()

    @property
    def pdf_url(self) -> str | None:
        return next((url for url in self.attachments if urlsplit(url).path.lower().endswith(".pdf")), None)


def page_url(slug: str) -> str:
    if not SLUG.fullmatch(slug):
        raise FetchError(f"ogiltig MFN-slug: {slug!r}")
    return f"{BASE_URL}/all/a/{slug}"


def normalize_url(url: str) -> str:
    """Jämförbar form: https, www.mfn.se, utan fråga, fragment och avslutande snedstreck."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host == "mfn.se":
        host = "www.mfn.se"
    return f"{parts.scheme or 'https'}://{host}{parts.path.rstrip('/')}"


def slug_from_url(url: str | None) -> str | None:
    """Bolagets slug ur en MFN-länk.

    Länkarna har formen /<kanal>/a/<slug>/<post>, där kanalen är valfri (cis, one, beq, eqs,
    nir, ...), eller /all/a/<slug> för bolagets egen sida. Utan kanal: /a/<slug>/<post>.
    """
    parts = urlsplit(url or "")
    if not (parts.hostname or "").lower().endswith("mfn.se"):
        return None
    segments = [part for part in parts.path.split("/") if part]
    marker = next((i for i, part in enumerate(segments[:2]) if part == "a"), None)
    if marker is None or len(segments) < marker + 2:
        return None
    listing = segments[:marker] == ["all"] and len(segments) == marker + 2
    if not (listing or len(segments) >= marker + 3):
        return None
    slug = segments[marker + 1]
    return slug if SLUG.fullmatch(slug) else None


def detect_language(title: str) -> str:
    """Grov gissning på rubrikens språk; MFN anger inget språk på listsidan."""
    swedish, english = len(SWEDISH.findall(title)), len(ENGLISH.findall(title))
    return "en" if english > swedish else "sv"


def _last_sunday(year: int, month: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() + 1) % 7)


def stockholm_iso(local: datetime) -> str:
    """Svensk lokal tid -> ISO med offset (sommartid från sista söndagen i mars till oktober)."""
    start = datetime.combine(_last_sunday(local.year, 3), datetime.min.time()).replace(hour=2)
    end = datetime.combine(_last_sunday(local.year, 10), datetime.min.time()).replace(hour=3)
    offset = 2 if start <= local < end else 1
    return f"{local.isoformat()}+{offset:02d}:00"


class _PageParser(HTMLParser):
    """Samlar posterna på sidan; en post slutar när dess `div` stängs."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: list[MfnItem] = []
        self._item: dict | None = None
        self._depth = 0
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "div" and "short-item" in classes:
            self._finish()
            self._item = {"attachments": []}
            self._depth = 1
        elif self._item is None:
            return
        elif tag == "div":
            self._depth += 1
        elif tag == "span":
            self._span(classes)
        elif tag == "a":
            self._link(classes, attributes)

    def _span(self, classes: set[str]) -> None:
        if "compressed-date" in classes:
            self._field = "date"
        elif "compressed-time" in classes:
            self._field = "time"
        elif classes & CATEGORY_CLASSES:
            self._field = "category"

    def _link(self, classes: set[str], attributes: dict[str, str]) -> None:
        if "item-link" in classes:
            self._item["href"] = attributes.get("href", "")
            self._item["title"] = attributes.get("title", "")
            self._field = "title" if not self._item["title"] else None
        elif "attachment-icon" in classes and attributes.get("href"):
            self._item["attachments"].append(urljoin(self.base_url, attributes["href"]))

    def handle_endtag(self, tag):
        if tag in ("span", "a"):
            self._field = None
        elif tag == "div" and self._item is not None:
            self._depth -= 1
            if self._depth <= 0:
                self._finish()

    def handle_data(self, data):
        if self._item is not None and self._field and data.strip():
            joined = f"{self._item.get(self._field, '')} {data.strip()}"
            self._item[self._field] = joined.strip()

    def close(self):
        super().close()
        self._finish()

    def _finish(self) -> None:
        item, self._item, self._field = self._item, None, None
        if not item or not all(item.get(key) for key in ("date", "time", "title", "href")):
            return
        try:
            local = datetime.strptime(f"{item['date']} {item['time']}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return
        title = " ".join(item["title"].split())
        self.items.append(MfnItem(
            title=title, published_at=stockholm_iso(local),
            url=normalize_url(urljoin(self.base_url, item["href"])), language=detect_language(title),
            category=item.get("category"), attachments=tuple(dict.fromkeys(item["attachments"]))))


def parse_page(html: str, base_url: str = BASE_URL) -> list[MfnItem]:
    parser = _PageParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.items


class Throttle:
    """Minst `interval` sekunder mellan anrop från samma arbetstråd."""

    def __init__(self, interval: float = MIN_INTERVAL) -> None:
        self.interval = interval
        self._local = threading.local()

    def wait(self) -> None:
        last = getattr(self._local, "last", None)
        if last is not None:
            time.sleep(max(0.0, self.interval - (time.monotonic() - last)))
        self._local.last = time.monotonic()


def fetch_html(url: str, settings: Settings, throttle: Throttle | None = None) -> str:
    if not allowed(url):
        raise FetchError(f"värden är inte tillåten: {urlsplit(url).hostname}")
    if throttle is not None:
        throttle.wait()
    request = Request(url, headers={"User-Agent": settings.user_agent,
                                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    with urlopen(request, timeout=settings.http_timeout) as response:  # noqa: S310 (värden är kontrollerad)
        if not allowed(response.geturl()):
            raise FetchError(f"omdirigerad till otillåten värd: {urlsplit(response.geturl()).hostname}")
        body = response.read(MAX_PAGE_BYTES + 1)
    if len(body) > MAX_PAGE_BYTES:
        raise FetchError("sidan är för stor")
    return body.decode("utf-8", errors="replace")


def fetch_items(slug: str, settings: Settings, throttle: Throttle | None = None) -> list[MfnItem]:
    """Bolagets senaste poster (sidan visar ca 50). En sida utan poster räknas som fel."""
    items = parse_page(fetch_html(page_url(slug), settings, throttle))
    if not items:
        raise FetchError(f"MFN-sidan för {slug} gav inga tolkbara poster")
    return items
