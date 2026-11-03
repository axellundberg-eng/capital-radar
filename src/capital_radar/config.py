"""Miljöstyrd konfiguration.

Alla värden har fungerande defaults lokalt. I drift sätts de i `.env` på
servern (läses av docker compose), aldrig i Git.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    documents_path: Path
    git_sha: str
    deepseek_api_key: str

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("CAPITAL_RADAR_DATA_DIR", "data"))
        return cls(
            data_dir=data_dir,
            # Samma filnamn som i den gamla tjänsten så att databasen kan
            # kopieras över oförändrad.
            db_path=Path(os.getenv("CAPITAL_RADAR_DB_PATH", data_dir / "screener.db")),
            documents_path=Path(
                os.getenv("CAPITAL_RADAR_DOCUMENTS_PATH", data_dir / "documents")
            ),
            git_sha=os.getenv("GIT_SHA", "dev"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        )
