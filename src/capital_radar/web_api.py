"""Webbens JSON-API och statiska filer.

Porterat från gamla `screener.web` (http.server) till FastAPI. Endast de vägar
som webbgränssnittet använder för översikt, bolagsradar, runway,
transaktionsflöde och Market Health finns kvar; garantier, CRM, agentköer och
nyhetskommentarer är borttagna. Svarsformaten är oförändrade så att
`workspace.js` fungerar som tidigare.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from importlib import resources
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .api_auth import ADMIN_PERMISSIONS, ApiError, AuthContext, extract_auth, require_admin
from .config import Settings
from .db import Database
from .issuer_repository import IssuerRepository
from .presentation_repository import PresentationRepository
from .radar_analysis import current_analysis, market_health, save_weight
from .research_channel import ResearchChannel
from .transaction_case import get_transaction_case
from .transaction_editorial import apply_cached_editorial
from .transaction_feed import _with_market_reaction, get_transaction_detail, get_transaction_feed
from .transaction_read_model import TransactionReadModel
from .transaction_refresh import TransactionEditorialWorker

logger = logging.getLogger(__name__)

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/workspace.js": "workspace.js",
    "/admin-auth.js": "admin-auth.js",
    **{f"/inter-{weight}.ttf": f"inter-{weight}.ttf" for weight in (400, 500, 600, 700)},
}
_RADAR_PRO_FORMA_SORTS = {
    "reported_runway": "reported_runway_months",
    "adjusted_runway": "adjusted_runway_months",
    "delta_runway": "delta_runway_months",
    "cash": "cash",
}


@dataclass
class WebServices:
    settings: Settings
    database: Database
    presenter: PresentationRepository
    issuers: IssuerRepository
    research: ResearchChannel
    read_model: TransactionReadModel | None = None
    editorial_worker: TransactionEditorialWorker | None = None

    @classmethod
    def build(cls, settings: Settings) -> "WebServices":
        return cls(
            settings=settings,
            database=Database(settings.db_path),
            presenter=PresentationRepository(
                settings.db_path, mfn_parallel_channels=settings.mfn_parallel_channels,
            ),
            issuers=IssuerRepository(settings.db_path),
            research=ResearchChannel(settings),
        )


def _json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _projected(payload: dict[str, Any]) -> JSONResponse:
    """202 medan läsmodellen ännu inte har publicerat någon generation."""
    metadata = payload.get("read_model")
    pending = isinstance(metadata, dict) and metadata.get("generation") is None
    return _json(payload, 202 if pending else 200)


def _read_connection(services: WebServices) -> sqlite3.Connection:
    connection = services.issuers.connect()
    connection.execute("BEGIN")
    return connection


def _sort_radar_issuers(
    issuers: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    *,
    sort: str,
    direction: str,
) -> list[dict[str, Any]]:
    """Sortera bolag på samma pro forma-värden som visas i Bolagsradar."""
    field = _RADAR_PRO_FORMA_SORTS[sort]
    analysis_by_issuer = {int(item["issuer_id"]): item for item in analyses}
    populated: list[tuple[float, str, int, dict[str, Any]]] = []
    cash_flow_positive: list[tuple[str, int, dict[str, Any]]] = []
    missing: list[tuple[str, int, dict[str, Any]]] = []
    for original in issuers:
        item = dict(original)
        issuer_id = int(item["id"])
        pro_forma = analysis_by_issuer.get(issuer_id, {"issuer_id": issuer_id})
        item["pro_forma"] = pro_forma
        value = pro_forma.get(field)
        name_key = str(item.get("name") or "").casefold()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if (field in {"reported_runway_months", "adjusted_runway_months"}
                    and pro_forma.get("cash_flow_positive") is True):
                cash_flow_positive.append((name_key, issuer_id, item))
                continue
            missing.append((name_key, issuer_id, item))
            continue
        populated.append((float(value), name_key, issuer_id, item))
    multiplier = -1 if direction == "desc" else 1
    populated.sort(key=lambda entry: (multiplier * entry[0], entry[1], entry[2]))
    cash_flow_positive.sort(key=lambda entry: (entry[0], entry[1]))
    missing.sort(key=lambda entry: (entry[0], entry[1]))
    positive_items = [entry[2] for entry in cash_flow_positive]
    populated_items = [entry[3] for entry in populated]
    missing_items = [entry[2] for entry in missing]
    if direction == "desc":
        return positive_items + populated_items + missing_items
    return populated_items + positive_items + missing_items


def _analyses(services: WebServices, issuer_ids: list[int] | None) -> tuple[list[dict[str, Any]], Any]:
    if services.read_model is not None:
        projection = services.read_model.analysis(issuer_ids)
        return projection["items"], projection["read_model"]
    with closing(_read_connection(services)) as connection:
        return current_analysis(connection, issuer_ids), None


def _pro_forma_sorted_issuers(services: WebServices, filters: dict[str, Any]) -> dict[str, Any]:
    presenter = services.presenter
    limit = max(1, min(int(filters.get("limit") or 50), 100))
    sort = str(filters.get("sort") or "")
    direction = str(filters.get("direction") or "asc").casefold()
    if direction not in {"asc", "desc"}:
        direction = "asc"

    base_filters = dict(filters)
    base_filters.update({"limit": 100, "sort": "name", "direction": "asc"})
    base_filters.pop("cursor", None)
    issuers: list[dict[str, Any]] = []
    base_cursor: str | None = None
    while True:
        if base_cursor:
            base_filters["cursor"] = base_cursor
        else:
            base_filters.pop("cursor", None)
        page = presenter.list_issuers(base_filters)
        issuers.extend(page["items"])
        base_cursor = page.get("next_cursor")
        if not base_cursor:
            break

    analyses, metadata = _analyses(services, [int(item["id"]) for item in issuers])
    ordered = _sort_radar_issuers(issuers, analyses, sort=sort, direction=direction)

    start = 0
    cursor = presenter._decode_cursor(filters.get("cursor"))
    cursor_marker = f"radar:{sort}:{direction}"
    if cursor and cursor.get("value") == cursor_marker:
        after_id = int(cursor["id"])
        start = next(
            (index + 1 for index, item in enumerate(ordered) if int(item["id"]) == after_id),
            0,
        )
    visible = ordered[start:start + limit]
    has_more = start + len(visible) < len(ordered)
    next_cursor = None
    if has_more and visible:
        next_cursor = presenter._encode_cursor(cursor_marker, int(visible[-1]["id"]))
    result: dict[str, Any] = {
        "items": visible,
        "next_cursor": next_cursor,
        "total_filtered": len(ordered),
        "sort": sort,
        "direction": direction,
    }
    if metadata is not None:
        result["read_model"] = metadata
    return result


def _editorial_request(services: WebServices, transaction_id: str) -> dict[str, Any] | None:
    worker = services.editorial_worker
    status = worker.request_status(transaction_id) if worker else None
    return status if isinstance(status, dict) else None


def _parse_issuer_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    ids = [int(value) for value in raw.split(",")]
    if len(ids) > 100 or any(value < 1 for value in ids):
        raise ValueError("Ogiltigt bolagsurval")
    return ids


def build_web_router() -> APIRouter:
    router = APIRouter()

    def services(request: Request) -> WebServices:
        return request.app.state.web

    @router.get("/api/auth/capabilities", tags=["auth"])
    def capabilities(request: Request) -> JSONResponse:
        """Används av admin-auth.js för att verifiera token före en skrivning."""
        auth = extract_auth(request)
        if not auth.authenticated:
            return _json({"error": "Giltig Bearer-token krävs", "code": "auth_required"}, 401)
        return _json({"actor": auth.actor, "role": auth.role, "permissions": list(ADMIN_PERMISSIONS)})

    @router.get("/api/overview", tags=["översikt"])
    def overview(web: WebServices = Depends(services)) -> JSONResponse:
        return _json(web.presenter.overview())

    @router.get("/api/issuer-markets", tags=["bolag"])
    def issuer_markets(web: WebServices = Depends(services)) -> JSONResponse:
        return _json({"items": web.presenter.list_markets()})

    @router.get("/api/issuers", tags=["bolag"])
    def issuers(request: Request, web: WebServices = Depends(services)) -> JSONResponse:
        try:
            filters: dict[str, Any] = dict(request.query_params)
            for key in ("limit", "fit_min"):
                if key in filters:
                    filters[key] = int(filters[key])
            if "runway_max" in filters:
                filters["runway_max"] = float(filters["runway_max"])
            if filters.get("sort") in _RADAR_PRO_FORMA_SORTS:
                return _projected(_pro_forma_sorted_issuers(web, filters))
            return _json(web.presenter.list_issuers(filters))
        except (TypeError, ValueError):
            return _json({"error": "Ogiltiga bolagsfilter"}, 400)

    @router.get("/api/issuer/{issuer_id}", tags=["bolag"])
    def issuer(issuer_id: str, web: WebServices = Depends(services)) -> JSONResponse:
        try:
            parsed = int(issuer_id)
        except ValueError:
            return _json({"error": "Ogiltigt bolags-ID"}, 400)
        payload = web.issuers.issuer(parsed)
        if payload is None:
            return _json({"error": "Bolaget hittades inte"}, 404)
        payload.update(web.presenter.issuer_extensions(parsed))
        return _json(payload)

    @router.get("/api/radar/analysis", tags=["radar"])
    def radar_analysis(issuer_ids: str | None = None, web: WebServices = Depends(services)) -> JSONResponse:
        try:
            ids = _parse_issuer_ids(issuer_ids)
            if web.read_model is not None:
                return _projected(web.read_model.analysis(ids))
            with closing(_read_connection(web)) as connection:
                return _projected({"items": current_analysis(connection, ids)})
        except ValueError:
            return _json({"error": "Ogiltigt bolagsurval"}, 400)
        except sqlite3.Error:
            return _json({"error": "Analysunderlaget kunde inte hämtas"}, 503)

    @router.get("/api/market-health", tags=["radar"])
    def health_overview(web: WebServices = Depends(services)) -> JSONResponse:
        try:
            if web.read_model is not None:
                return _projected(web.read_model.health())
            with closing(_read_connection(web)) as connection:
                return _projected(market_health(connection))
        except sqlite3.Error:
            return _json({"error": "Analysunderlaget kunde inte hämtas"}, 503)

    @router.post("/api/radar/preview", tags=["radar"])
    def radar_preview(payload: dict[str, Any] = Body(...), web: WebServices = Depends(services)) -> JSONResponse:
        """Tillfällig simulering i en läs-snapshot; sparar ingenting."""
        try:
            issuer_id = int(payload.get("issuer_id", 0))
            weights = payload.get("weights", {})
            if not isinstance(weights, dict) or len(weights) > 100 or issuer_id < 1:
                raise ValueError("Ogiltig simulering")
            with closing(_read_connection(web)) as connection:
                return _json({"items": current_analysis(connection, [issuer_id], weights)})
        except (ValueError, TypeError):
            return _json({"error": "Ogiltig vikt eller ändringsorsak"}, 400)
        except KeyError:
            return _json({"error": "Affären hittades inte"}, 404)
        except sqlite3.Error:
            return _json({"error": "Analysen kunde inte uppdateras"}, 503)

    @router.post("/api/radar/weight", tags=["radar"])
    def radar_weight(
        payload: dict[str, Any] = Body(...),
        auth: AuthContext = Depends(require_admin),
        web: WebServices = Depends(services),
    ) -> JSONResponse:
        try:
            if payload.get("confirm") is not True:
                raise ValueError("Bekräfta ändringen")
            with web.database.connect() as connection:
                result = save_weight(
                    connection, payload.get("transaction_id"), payload.get("weight"),
                    actor=auth.actor, reason=payload.get("reason"),
                )
            return _json({"items": result})
        except (ValueError, TypeError):
            return _json({"error": "Ogiltig vikt eller ändringsorsak"}, 400)
        except KeyError:
            return _json({"error": "Affären hittades inte"}, 404)
        except sqlite3.Error:
            return _json({"error": "Analysen kunde inte uppdateras"}, 503)

    @router.get("/api/operations/summary", tags=["drift"])
    def operations_summary(web: WebServices = Depends(services)) -> JSONResponse:
        settings = web.settings
        payload = web.presenter.operations_summary()
        payload["automation"] = {
            "enabled": settings.auto_analysis_enabled,
            "policy_version": "autonomous-analysis-v1",
            "poll_interval_seconds": settings.auto_analysis_interval_seconds,
            "queue_target": settings.auto_analysis_queue_target,
            "max_attempts_per_day": settings.auto_analysis_max_attempts,
            "runway_tolerance_months": 1,
            "missing_facts_policy": "await_sources_and_retry",
        }
        return _json(payload)

    @router.get("/api/research/status", tags=["research"])
    def research_status(web: WebServices = Depends(services)) -> JSONResponse:
        return _json(web.research.status())

    @router.get("/api/research/history", tags=["research"])
    def research_history() -> JSONResponse:
        """Research-historiken är inte porterad; svaret är alltid tomt."""
        return _json({"items": [], "enabled": False})

    @router.get("/api/transactions", tags=["transaktioner"])
    def transactions(request: Request, web: WebServices = Depends(services)) -> JSONResponse:
        query = request.query_params
        try:
            page = int(query.get("page", "1"))
            page_size = int(query.get("page_size", "10"))
            if not 1 <= page <= 100_000 or page_size != 10:
                raise ValueError("Invalid pagination")
            feed_arguments = dict(
                page=page, page_size=page_size,
                q=query.get("q", ""), category=query.get("category", ""),
                stage=query.get("stage", ""), advisor=query.get("advisor", ""),
                agent=query.get("agent", ""), from_date=query.get("from", ""),
            )
            payload = web.read_model.feed(**feed_arguments) if web.read_model is not None else None
            # Läser bara de tio synliga redaktionella posterna.
            with closing(_read_connection(web)) as connection:
                if payload is None:
                    payload = get_transaction_feed(connection, **feed_arguments)
                payload["items"] = [apply_cached_editorial(connection, item) for item in payload["items"]]
            return _projected(payload)
        except (ValueError, TypeError):
            return _json(
                {"error": "Ogiltig sida. Visa tio transaktioner per sida.", "code": "invalid_transaction_page"},
                400,
            )
        except sqlite3.Error:
            logger.warning("transaction_feed_unavailable")
            return _json(
                {"error": "Transaktionerna kunde inte hämtas. Försök igen.", "code": "transaction_feed_unavailable"},
                503,
            )

    @router.get("/api/transactions/{transaction_id}/case", tags=["transaktioner"])
    def transaction_case(transaction_id: str, web: WebServices = Depends(services)) -> JSONResponse:
        try:
            with closing(web.issuers.connect()) as connection:
                payload = get_transaction_case(connection, transaction_id)
                payload["transaction"] = apply_cached_editorial(connection, payload["transaction"])
            payload["transaction"]["editorial_request"] = _editorial_request(web, transaction_id)
            return _json(payload)
        except ValueError:
            return _json({"error": "Ogiltigt transaktions-ID", "code": "invalid_transaction_id"}, 400)
        except KeyError:
            return _json({"error": "Transaktionen hittades inte", "code": "transaction_not_found"}, 404)
        except sqlite3.Error:
            logger.warning("transaction_case_unavailable")
            return _json(
                {"error": "Transaktionsakten kunde inte hämtas. Försök igen.", "code": "transaction_case_unavailable"},
                503,
            )

    @router.post("/api/transactions/{transaction_id}/editorial", tags=["transaktioner"])
    def transaction_editorial(
        transaction_id: str,
        _auth: AuthContext = Depends(require_admin),
        web: WebServices = Depends(services),
    ) -> JSONResponse:
        """Prioriterar en AI-sammanfattning; HTTP-anropet anropar aldrig leverantören."""
        try:
            with closing(_read_connection(web)) as connection:
                item = get_transaction_detail(connection, transaction_id)
        except (ValueError, KeyError):
            return _json({"error": "Transaktionen hittades inte"}, 404)
        worker = web.editorial_worker
        queued = worker.request(item) if worker else False
        status = _editorial_request(web, transaction_id) or {"status": "queued" if queued else "unavailable"}
        return _json(status, 202 if queued else 503)

    @router.get("/api/transactions/{transaction_id}", tags=["transaktioner"])
    def transaction_detail(transaction_id: str, web: WebServices = Depends(services)) -> JSONResponse:
        try:
            item = web.read_model.detail(transaction_id) if web.read_model is not None else None
            if item is not None and item["read_model"]["generation"] is None:
                return _projected(item)
            with closing(_read_connection(web)) as connection:
                if item is None:
                    item = get_transaction_detail(connection, transaction_id)
                else:
                    item = _with_market_reaction(connection, item)
                item = apply_cached_editorial(connection, item)
            item["editorial_request"] = _editorial_request(web, transaction_id)
            return _projected(item)
        except ValueError:
            return _json({"error": "Ogiltigt transaktions-ID", "code": "invalid_transaction_id"}, 400)
        except KeyError:
            return _json({"error": "Transaktionen hittades inte", "code": "transaction_not_found"}, 404)
        except sqlite3.Error:
            logger.warning("transaction_detail_unavailable")
            return _json(
                {"error": "Transaktionen kunde inte hämtas. Försök igen.", "code": "transaction_detail_unavailable"},
                503,
            )

    return router


def build_static_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)
    web_root = resources.files("capital_radar") / "web"

    def serve(path: str) -> Response:
        file_name = _STATIC_FILES[path]
        with resources.as_file(web_root / file_name) as target:
            # HTML hålls färsk så att den väljer aktuella versionerade filer.
            cache_control = "no-store" if file_name == "index.html" else "public, max-age=604800"
            return FileResponse(
                target,
                headers={
                    "Cache-Control": cache_control,
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": _CSP,
                },
            )

    for static_path in _STATIC_FILES:
        router.add_api_route(static_path, lambda path=static_path: serve(path), methods=["GET"])
    return router


def api_error_handler(_request: Request, error: ApiError) -> JSONResponse:
    return _json(error.payload, error.status_code)
