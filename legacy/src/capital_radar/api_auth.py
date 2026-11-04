"""Administratörsbehörighet för webbens skrivningar.

Läsningar är publika i appen; själva webbplatsen skyddas av Cloudflare Access.
Skrivningar (sparade vikter, prioriterade AI-sammanfattningar) kräver en
Bearer-token som matchar SCREENER_ADMIN_API_TOKEN, SCREENER_API_TOKEN eller
någon i den kommaseparerade listan SCREENER_API_TOKENS. Inget token lagras.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import Request


class ApiError(Exception):
    """Ger ett JSON-svar i det gamla formatet {"error", "code"} via app-hanteraren."""

    def __init__(self, status_code: int, error: str, code: str | None = None) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.payload: dict[str, str] = {"error": error}
        if code:
            self.payload["code"] = code


@dataclass(frozen=True)
class AuthContext:
    authenticated: bool
    actor: str
    role: str


ANONYMOUS = AuthContext(False, "anonymous", "anonymous")
ADMIN_PERMISSIONS = ("admin", "read")


def _admin_tokens() -> set[str]:
    tokens = {
        os.getenv("SCREENER_ADMIN_API_TOKEN", ""),
        os.getenv("SCREENER_API_TOKEN", ""),
        *os.getenv("SCREENER_API_TOKENS", "").split(","),
    }
    return {token.strip() for token in tokens if token.strip()}


def extract_auth(request: Request) -> AuthContext:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return ANONYMOUS
    token = authorization[7:].strip()
    if not token:
        return ANONYMOUS
    # Jämför digest-bytes så att även felaktig Unicode inte kan kasta.
    supplied = hashlib.sha256(token.encode()).digest()
    for candidate in _admin_tokens():
        if hmac.compare_digest(supplied, hashlib.sha256(candidate.encode()).digest()):
            return AuthContext(True, "api-user", "admin")
    return ANONYMOUS


def require_admin(request: Request) -> AuthContext:
    """FastAPI-beroende för skrivande endpoints."""
    auth = extract_auth(request)
    if not auth.authenticated:
        raise ApiError(401, "Giltig Bearer-token krävs för denna endpoint", "auth_required")
    return auth
