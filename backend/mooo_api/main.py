"""FastAPI application factory for the Mooo API Server.

The API Server does not run trading logic. It serves the dashboard API and relays
operator commands to the Agent Worker. Later work orders add the routers listed in
the API Server blueprint.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mooo_core import __version__
from mooo_core.config import Settings, load_settings_or_exit
from mooo_core.logging_setup import configure_logging

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


def error_body(error_code: str, message: str) -> dict[str, str]:
    """Build the standard error body. Messages must never include secrets."""
    return {"error_code": error_code, "message": message}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the API application. Loads settings from the environment when not given."""
    resolved: Settings
    if settings is None:
        resolved = load_settings_or_exit()
        configure_logging(resolved.log_level)
    else:
        resolved = settings

    docs_enabled = not resolved.is_production
    app = FastAPI(
        title="Mooo API",
        version=__version__,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = resolved

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "Request validation failed"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, _exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "Internal server error"),
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "api",
            "version": __version__,
            "app_env": resolved.app_env.value,
            "bybit_env": resolved.bybit_env.value,
        }

    logger.info(
        "Mooo API configured (app_env=%s, bybit_env=%s)",
        resolved.app_env.value,
        resolved.bybit_env.value,
    )
    return app
