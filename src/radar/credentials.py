"""De enda värden som delas med den gamla koden: API-inloggningar.

Allt annat (sökvägar, flaggor, gränser) har egna `RADAR_`-namn i den nya
koden. Listan nedan är den fullständiga tillåtna mängden; testet
`tests/test_boundaries.py` underkänner all annan användning av gamla namn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Miljövariabel -> vad den används till.
SHARED_CREDENTIALS = {
    "DEEPSEEK_API_KEY": "DeepSeek, extraktion och tolkning",
    "ANTHROPIC_API_KEY": "Claude Haiku, granskning av extraktion",
    "SCREENER_ADMIN_API_TOKEN": "Admininloggning för skrivningar i webben",
}


@dataclass(frozen=True)
class Credentials:
    deepseek_api_key: str | None
    anthropic_api_key: str | None
    admin_api_token: str | None

    @classmethod
    def from_env(cls) -> "Credentials":
        def read(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        return cls(
            deepseek_api_key=read("DEEPSEEK_API_KEY"),
            anthropic_api_key=read("ANTHROPIC_API_KEY"),
            admin_api_token=read("SCREENER_ADMIN_API_TOKEN"),
        )

    def __repr__(self) -> str:  # aldrig nycklar i loggar
        present = {name: value is not None for name, value in vars(self).items()}
        return f"Credentials({present})"
