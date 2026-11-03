"""Stabila hashar för idempotenta AI-anrop.

Utbrutna oförändrade ur agent_runner i Screener-analys-hus; samma värden
krävs för att befintliga extraction_jobs ska kännas igen.
"""

from __future__ import annotations

import hashlib
from typing import Optional


def compute_input_hash(
    *,
    agent_type: str,
    prompt_version: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """Stabil hash över allt som styr ett agent-anrop."""
    digest = hashlib.sha256()
    for part in (agent_type, prompt_version, model, system_prompt, user_prompt):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def compute_idempotency_key(
    *,
    agent_type: str,
    issuer_id: Optional[int],
    case_id: Optional[str],
    input_hash: str,
) -> str:
    """Nyckel som gör ett run idempotent för samma input och scope."""
    raw = f"{agent_type}|{issuer_id or ''}|{case_id or ''}|{input_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
