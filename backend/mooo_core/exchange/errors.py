"""Exchange error hierarchy (Exchange Adapter blueprint, BybitRestClient).

Bybit retCodes map to four ``ExchangeError`` subclasses:

* auth: 10003, 10004, 10005, 33004
* rate limit: 10006, 10018
* temporary: 10000, 10016 (and HTTP 5xx, timeouts, transport failures)
* rejection: every other non-zero retCode

``ForbiddenEndpointError`` is raised before any request is built when a path is not on the
allow-list.
"""

from typing import ClassVar

AUTH_RET_CODES = frozenset({10003, 10004, 10005, 33004})
RATE_LIMIT_RET_CODES = frozenset({10006, 10018})
IP_RATE_LIMIT_RET_CODES = frozenset({10018})
TEMPORARY_RET_CODES = frozenset({10000, 10016})


class ExchangeError(Exception):
    """Base class for errors raised by the exchange layer."""

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        ret_code: int | None = None,
        http_status: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.ret_code = ret_code
        self.http_status = http_status
        self.path = path

    def __str__(self) -> str:
        parts = [self.message]
        if self.ret_code is not None:
            parts.append(f"retCode={self.ret_code}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.path:
            parts.append(f"path={self.path}")
        return " ".join(parts)


class ForbiddenEndpointError(ExchangeError):
    """The method and path are not on the Bybit endpoint allow-list. Nothing was sent."""


class ExchangeAuthError(ExchangeError):
    """Bybit rejected the credentials, signature, or permissions."""


class ExchangeRateLimitError(ExchangeError):
    """Bybit reported a rate limit. ``reset_at_ms`` is Bybit's reset time when known."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        ret_code: int | None = None,
        http_status: int | None = None,
        path: str | None = None,
        reset_at_ms: int | None = None,
        ip_wide: bool = False,
    ) -> None:
        super().__init__(message, ret_code=ret_code, http_status=http_status, path=path)
        self.reset_at_ms = reset_at_ms
        self.ip_wide = ip_wide


class ExchangeTemporaryError(ExchangeError):
    """A temporary failure (server error, timeout, transport error). Safe to retry later."""

    retryable = True


class ExchangeRejectionError(ExchangeError):
    """Bybit rejected the request for a business reason. Do not retry unchanged."""


def error_for_ret_code(
    ret_code: int,
    message: str,
    *,
    http_status: int | None = None,
    path: str | None = None,
    reset_at_ms: int | None = None,
) -> ExchangeError:
    """Return the ``ExchangeError`` subclass instance for a non-zero Bybit retCode."""
    if ret_code in AUTH_RET_CODES:
        return ExchangeAuthError(message, ret_code=ret_code, http_status=http_status, path=path)
    if ret_code in RATE_LIMIT_RET_CODES:
        return ExchangeRateLimitError(
            message,
            ret_code=ret_code,
            http_status=http_status,
            path=path,
            reset_at_ms=reset_at_ms,
            ip_wide=ret_code in IP_RATE_LIMIT_RET_CODES,
        )
    if ret_code in TEMPORARY_RET_CODES:
        return ExchangeTemporaryError(
            message, ret_code=ret_code, http_status=http_status, path=path
        )
    return ExchangeRejectionError(message, ret_code=ret_code, http_status=http_status, path=path)
