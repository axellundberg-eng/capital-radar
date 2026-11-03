"""Lokal start: `python -m capital_radar` (server) eller
`python -m capital_radar review {extractions,headlines} [--dry-run]`."""

from __future__ import annotations

import os
import sys


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "capital_radar.app:create_app",
        factory=True,
        host=os.getenv("CAPITAL_RADAR_HOST", "127.0.0.1"),
        port=int(os.getenv("CAPITAL_RADAR_PORT", "8000")),
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "review":
        from .reviewer.cli import main as review_main

        return review_main(argv)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
