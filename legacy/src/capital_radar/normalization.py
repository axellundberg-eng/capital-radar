from __future__ import annotations

import hashlib
import re
import unicodedata
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_CORPORATE_SUFFIXES = re.compile(
    r"\b(?:ab|ab publ|oyj|oy|asa|as|a s|plc|ltd|limited|inc|corp|corporation)\b",
    re.IGNORECASE,
)
_SHARE_CLASS_SUFFIX = re.compile(
    r"(?:\s+(?:ser(?:ie)?\s*)?(?:a|b|c|d|pref(?:\s+[ab])?|sdb|sdr))$",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_issuer_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    text = text.replace("(publ)", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = _CORPORATE_SUFFIXES.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _SHARE_CLASS_SUFFIX.sub("", text).strip()
    return text


def issuer_display_name(value: str) -> str:
    text = _WHITESPACE.sub(" ", value).strip()
    text = re.sub(r"\s+\(publ\)$", "", text, flags=re.IGNORECASE)
    text = _SHARE_CLASS_SUFFIX.sub("", text).strip()
    return text


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in {"utm_source", "utm_medium", "utm_campaign", "ref"}
    ]
    return urlunsplit(
        (
            parts.scheme.casefold() or "https",
            parts.netloc.casefold(),
            re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def stable_hash(*parts: str | bytes | None) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update((part or "").encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def document_event_key(
    issuer_name: str, title: str, published_at: str, language: str | None
) -> str:
    published_day = published_at[:10]
    normalized_title = _WHITESPACE.sub(
        " ", unicodedata.normalize("NFKC", title).casefold()
    ).strip()
    return stable_hash(
        normalize_issuer_name(issuer_name),
        normalized_title,
        published_day,
    )


def isin_jurisdiction(isin: str) -> str | None:
    value = isin.strip().upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value):
        return value[:2]
    return None


def stockholm_timezone_for_local(value: datetime) -> timezone:
    """Return Sweden's UTC offset for a naive local timestamp.

    Windows Python installations do not always include the IANA tzdata package.
    Sweden follows the EU rule: DST starts the last Sunday in March and ends the
    last Sunday in October.
    """

    def last_sunday(year: int, month: int) -> int:
        last_day = monthrange(year, month)[1]
        weekday = datetime(year, month, last_day).weekday()
        return last_day - ((weekday - 6) % 7)

    starts = datetime(value.year, 3, last_sunday(value.year, 3), 2)
    ends = datetime(value.year, 10, last_sunday(value.year, 10), 3)
    offset = 2 if starts <= value < ends else 1
    return timezone(timedelta(hours=offset))
