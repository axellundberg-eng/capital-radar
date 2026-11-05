"""Inställningar för den nya kärnan. Alla namn börjar med RADAR_."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    files_path: Path
    legacy_db_path: Path
    deepseek_model: str
    deepseek_base_url: str
    http_timeout: float
    user_agent: str
    collect_source: str = "legacy"   # legacy: importera ur gamla databasen; mfn: samla själv

    @classmethod
    def from_env(cls) -> "Settings":
        data = Path(os.getenv("RADAR_DATA_PATH", "data"))
        return cls(
            db_path=Path(os.getenv("RADAR_DB_PATH", str(data / "radar.db"))),
            files_path=Path(os.getenv("RADAR_FILES_PATH", str(data / "radar-files"))),
            legacy_db_path=Path(os.getenv("RADAR_LEGACY_DB_PATH", str(data / "screener.db"))),
            deepseek_model=os.getenv("RADAR_DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_base_url=os.getenv("RADAR_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            http_timeout=float(os.getenv("RADAR_HTTP_TIMEOUT", "30")),
            user_agent=os.getenv("RADAR_USER_AGENT", "CapitalRadar/0.2"),
            collect_source="mfn" if os.getenv("RADAR_COLLECT_SOURCE", "legacy").strip().lower() == "mfn" else "legacy",
        )
