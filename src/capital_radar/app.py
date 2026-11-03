"""FastAPI-applikationen.

OpenAPI-specen genereras automatiskt från routrarna (/openapi.json, /docs).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .reviewer.api import build_review_router


def _database_status(settings: Settings) -> dict[str, object]:
    """Snabb, skrivskyddad kontroll. Läser aldrig tabellinnehåll."""
    if not settings.db_path.exists():
        return {"status": "missing"}
    try:
        uri = f"file:{settings.db_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error as error:
        return {"status": "error", "error": type(error).__name__}
    return {"status": "ok", "schema_version": schema_version, "journal_mode": journal_mode}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Capital Radar", version=__version__)
    app.state.settings = settings

    @app.get("/health", tags=["drift"])
    def health() -> JSONResponse:
        """Används av deploy-smoke-test och övervakning."""
        database = _database_status(settings)
        healthy = database["status"] == "ok"
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "version": __version__,
                "git_sha": settings.git_sha,
                "database": database,
            },
        )

    app.include_router(build_review_router(settings))

    return app
