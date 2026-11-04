"""Lokal start: `python -m capital_radar`."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=os.getenv("CAPITAL_RADAR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "capital_radar.app:create_app",
        factory=True,
        host=os.getenv("CAPITAL_RADAR_HOST", "127.0.0.1"),
        port=int(os.getenv("CAPITAL_RADAR_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
