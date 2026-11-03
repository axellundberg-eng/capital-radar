"""Lokal start: `python -m capital_radar`."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "capital_radar.app:create_app",
        factory=True,
        host=os.getenv("CAPITAL_RADAR_HOST", "127.0.0.1"),
        port=int(os.getenv("CAPITAL_RADAR_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
