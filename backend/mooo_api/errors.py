"""JSON error bodies for the API Server: ``{error_code, message}``.

Messages never echo request bodies, so passwords and keys cannot leak into
error responses.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

SERVICE_UNAVAILABLE_MESSAGE = "A required service is unavailable. Try again shortly."


class ApiError(Exception):
    """An error that the API returns to the client as ``{error_code, message}``."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.headers = headers


def error_body(error_code: str, message: str) -> dict[str, str]:
    return {"error_code": error_code, "message": message}


async def _api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.error_code, exc.message),
        headers=exc.headers,
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_body("validation_error", "The request body is not valid."),
    )


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(f"http_{exc.status_code}", str(exc.detail)),
        headers=exc.headers,
    )


async def _service_unavailable_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body("service_unavailable", SERVICE_UNAVAILABLE_MESSAGE),
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RedisError, _service_unavailable_handler)
    app.add_exception_handler(SQLAlchemyError, _service_unavailable_handler)
    app.add_exception_handler(OSError, _service_unavailable_handler)
