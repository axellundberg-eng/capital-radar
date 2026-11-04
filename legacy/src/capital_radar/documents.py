from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request

from pypdf import PdfReader

from .http import build_direct_https_opener, describe_network_error

LOGGER = logging.getLogger("bergs.ai")

# HTTP-statuskoder som är värda ett nytt försök (rate limit + serverfel).
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class DocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedDocument:
    source_url: str
    final_url: str
    mime_type: str
    body: bytes


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    method: str
    page_count: int
    status: str


def validate_document_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme.casefold() != "https":
        raise DocumentError("Dokumentlänken måste använda HTTPS")
    if parts.username or parts.password:
        raise DocumentError("Inloggningsuppgifter tillåts inte i dokumentlänkar")
    if hostname not in allowed_hosts:
        raise DocumentError(f"Dokumentvärden är inte godkänd: {hostname or '<tom>'}")
    if parts.port not in (None, 443):
        raise DocumentError("Endast standardporten för HTTPS tillåts")
    return hostname


def verify_public_addresses(hostname: str) -> None:
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise DocumentError(f"Kunde inte slå upp dokumentvärden {hostname}") from exc
    if not addresses:
        raise DocumentError(f"Dokumentvärden saknar IP-adress: {hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise DocumentError(f"Dokumentvärden pekar mot en icke-publik adress: {ip}")


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self.allowed_hosts = allowed_hosts
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        hostname = validate_document_url(newurl, self.allowed_hosts)
        verify_public_addresses(hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeDocumentFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        user_agent: str,
        timeout: float,
        max_bytes: int,
        retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = max(0, retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._opener = build_direct_https_opener(
            _ValidatedRedirectHandler(allowed_hosts),
        )

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """Avgör om ett nätverksfel är transient och värt ett nytt försök.

        Permanenta fel (4xx utom 429, valideringsfel) retryas aldrig.
        """
        if isinstance(exc, HTTPError):
            return exc.code in _RETRYABLE_HTTP_STATUSES
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, URLError):
            reason = getattr(exc, "reason", None)
            # Timeout/connection-reset är transient; DNS- och andra permanenta
            # fel (t.ex. 404 via reason) retryas inte.
            return isinstance(reason, (TimeoutError, socket.timeout, ConnectionError))
        if isinstance(exc, ConnectionError):
            return True
        return False

    def get(self, url: str) -> DownloadedDocument:
        hostname = validate_document_url(url, self.allowed_hosts)
        verify_public_addresses(hostname)
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.1",
            },
        )
        attempts = self.retries + 1
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._fetch_once(request, url)
            except DocumentError:
                # Validerings- och innehållsfel är permanenta.
                raise
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt >= attempts:
                    LOGGER.warning(
                        "doc_fetch_failed url=%s attempt=%d/%d error=%s",
                        url,
                        attempt,
                        attempts,
                        describe_network_error(exc),
                    )
                    raise DocumentError(
                        "Dokumenthämtning misslyckades för "
                        f"{url}: {describe_network_error(exc)}"
                    ) from exc
                delay = self.retry_backoff * (2 ** (attempt - 1))
                LOGGER.info(
                    "doc_fetch_retry url=%s attempt=%d/%d retry_in=%.2fs error=%s",
                    url,
                    attempt,
                    attempts,
                    delay,
                    describe_network_error(exc),
                )
                time.sleep(delay)
        # Defensivt: loopen returnerar eller kastar alltid, men mypy kräver detta.
        raise DocumentError(
            f"Dokumenthämtning misslyckades för {url}: {describe_network_error(last_error)}"
        )

    def _fetch_once(self, request: Request, url: str) -> DownloadedDocument:
        with self._opener.open(request, timeout=self.timeout) as response:
            final_url = response.geturl()
            final_host = validate_document_url(final_url, self.allowed_hosts)
            verify_public_addresses(final_host)
            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > self.max_bytes:
                raise DocumentError("Dokumentet överskrider storleksgränsen")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, self.max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_bytes:
                    raise DocumentError("Dokumentet överskrider storleksgränsen")
            body = b"".join(chunks)
            mime_type = response.headers.get_content_type()
        if not body:
            raise DocumentError("Dokumentet var tomt")
        return DownloadedDocument(url, final_url, mime_type, body)


class _ReadableHtmlParser(HTMLParser):
    BLOCKED = {"script", "style", "svg", "noscript", "nav", "header", "footer"}
    BREAKS = {
        "br", "p", "div", "article", "section", "li", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag in self.BLOCKED:
            self._blocked_depth += 1
        elif tag in self.BREAKS and self._blocked_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCKED:
            self._blocked_depth = max(0, self._blocked_depth - 1)
        elif tag in self.BREAKS and self._blocked_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._blocked_depth == 0:
            self.parts.append(data)


def extract_document(
    body: bytes, mime_type: str | None, source_url: str
) -> ExtractedDocument:
    is_pdf = (
        body.startswith(b"%PDF")
        or (mime_type or "").casefold() == "application/pdf"
        or source_url.casefold().endswith(".pdf")
    )
    if is_pdf:
        try:
            reader = PdfReader(BytesIO(body))
            pages = []
            for number, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text(extraction_mode="layout") or ""
                pages.append(f"[PAGE {number}]\n{_clean_text(page_text)}")
            text = "\n\n".join(pages).strip()
        except Exception as exc:
            raise DocumentError(f"PDF-texten kunde inte extraheras: {exc}") from exc
        return ExtractedDocument(
            text=text,
            method="pypdf_layout",
            page_count=len(reader.pages),
            status="extracted" if len(text) >= 500 else "needs_ocr",
        )

    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = body.decode("latin-1", errors="replace")
    parser = _ReadableHtmlParser()
    parser.feed(decoded)
    text = _clean_text("".join(parser.parts))
    if not text:
        raise DocumentError("HTML-sidan saknade läsbar text")
    return ExtractedDocument(
        text=f"[PAGE 1]\n{text}",
        method="html_parser",
        page_count=1,
        status="extracted" if len(text) >= 200 else "too_short",
    )


def persist_download(
    root: Path,
    *,
    issuer_id: int,
    document_id: int,
    body: bytes,
    mime_type: str,
) -> tuple[Path, str]:
    digest = hashlib.sha256(body).hexdigest()
    suffix = (
        ".pdf"
        if body.startswith(b"%PDF") or mime_type == "application/pdf"
        else ".html"
    )
    target = root / str(issuer_id) / str(document_id) / f"{digest}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(body)
    return target, digest


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()
