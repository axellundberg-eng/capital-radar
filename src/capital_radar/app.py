"""FastAPI-applikationen.

OpenAPI-specen genereras automatiskt från routrarna (/openapi.json, /docs).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .api_auth import ApiError
from .config import Settings
from .radar_analysis import SnapshotObserver
from .reviewer.api import build_review_router
from .transaction_read_model import TransactionReadModel
from .transaction_refresh import TransactionEditorialWorker
from .web_api import WebServices, api_error_handler, build_static_router, build_web_router


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


def create_app(settings: Settings | None = None, *, start_workers: bool = True) -> FastAPI:
    """`start_workers=False` ger en app utan bakgrundstrådar (för tester).

    Utan läsmodell läser API:t direkt ur databasen, som gamla servern gjorde.
    """
    settings = settings or Settings.from_env()
    web = WebServices.build(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not start_workers:
            yield
            return
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        web.database.initialize()
        web.read_model = TransactionReadModel(settings.db_path)
        web.editorial_worker = TransactionEditorialWorker(web.database, settings)
        snapshot_observer = SnapshotObserver(web.database)
        web.read_model.start()
        web.editorial_worker.start()
        snapshot_observer.start()
        try:
            yield
        finally:
            snapshot_observer.stop()
            web.editorial_worker.stop()
            web.read_model.stop()

    app = FastAPI(title="Capital Radar", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.web = web
    app.add_exception_handler(ApiError, api_error_handler)

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

    app.include_router(build_web_router())
    app.include_router(build_review_router(settings))
    app.include_router(build_static_router())
    return app
