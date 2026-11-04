"""Hämta rapport-PDF:er. Filer sparas permanent och raderas aldrig."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALLOWED_HOSTS = ("mfn.se", "cision.com", "bequoted.com", "globenewswire.com", "beqom.com")
MAX_BYTES = 40_000_000


class FetchError(RuntimeError):
    pass


def allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def fetch_pdf(url: str, target: Path, *, timeout: float, user_agent: str) -> tuple[Path, str]:
    if not allowed(url):
        raise FetchError(f"värden är inte tillåten: {urlparse(url).hostname}")
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/pdf"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (värden är kontrollerad)
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise FetchError("filen är för stor")
    if not body.startswith(b"%PDF"):
        raise FetchError("svaret är ingen PDF")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target, hashlib.sha256(body).hexdigest()
