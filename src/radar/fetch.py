"""Hämta rapport-PDF:er. Filer sparas permanent och raderas aldrig."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALLOWED_HOSTS = ("mfn.se", "tt.se", "cision.com", "bequoted.com", "globenewswire.com", "beqom.com",
                 "storage.googleapis.com")   # vissa bolag (t.ex. PION) lägger rapport-PDF:en där
MAX_BYTES = 40_000_000
MAX_TEXT_BYTES = 2_000_000
MAX_TEXT_CHARS = 12_000
SKIPPED_TAGS = ("script", "style", "noscript", "svg", "head", "template")


class FetchError(RuntimeError):
    pass


def allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def fetch_pdf(url: str, target: Path, *, timeout: float, user_agent: str) -> tuple[Path, str]:
    if not allowed(url):
        raise FetchError(f"värden är inte tillåten: {urlparse(url).hostname}")
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*;q=0.8"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (värden är kontrollerad)
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise FetchError("filen är för stor")
    if not body.startswith(b"%PDF"):
        raise FetchError("svaret är ingen PDF")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target, hashlib.sha256(body).hexdigest()


class _Text(HTMLParser):
    """Samlar synlig text och hoppar över script, style och liknande."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED_TAGS:
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIPPED_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def visible_text(html: str, limit: int = MAX_TEXT_CHARS) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)[:limit]


def fetch_text(url: str, *, timeout: float, user_agent: str) -> str:
    """Synlig text ur en HTML-sida (högst ca 2 MB hämtas, 12 000 tecken returneras)."""
    if not allowed(url):
        raise FetchError(f"värden är inte tillåten: {urlparse(url).hostname}")
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (värden är kontrollerad)
        if not allowed(response.geturl()):
            raise FetchError(f"omdirigerad till otillåten värd: {urlparse(response.geturl()).hostname}")
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read(MAX_TEXT_BYTES + 1)
    if len(body) > MAX_TEXT_BYTES:
        body = body[:MAX_TEXT_BYTES]  # stora sidor klipps; pressmeddelandets början räcker
    text = visible_text(body.decode(charset, errors="replace"))
    if not text:
        raise FetchError("sidan saknar text")
    return text
