"""Exchange error types and Bybit retCode mapping (Exchange Adapter blueprint)."""

AUTH_RET_CODES = frozenset({10003, 10004, 10005, 33004})
RATE_LIMIT_RET_CODES = frozenset({10006, 10018})
TEMPORARY_RET_CODES = frozenset({10000, 10016})


class ExchangeError(Exception):
    """Base class for every error raised by the exchange layer."""

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

    @property
    def retryable(self) -> bool:
        return False


class ForbiddenEndpointError(ExchangeError):
    """The path is not on the endpoint allow-list. No request was sent."""


class ExchangeAuthError(ExchangeError):
    """The API credential is missing, invalid, expired, or lacks permission."""


class ExchangeRateLimitError(ExchangeError):
    """Bybit reported a rate limit. ``reset_at`` is the epoch second when it lifts."""

    def __init__(
        self,
        message: str,
        *,
        ret_code: int | None = None,
        http_status: int | None = None,
        path: str | None = None,
        reset_at: float | None = None,
    ) -> None:
        super().__init__(message, ret_code=ret_code, http_status=http_status, path=path)
        self.reset_at = reset_at

    @property
    def retryable(self) -> bool:
        return True


class ExchangeTemporaryError(ExchangeError):
    """A temporary failure: server error, HTTP 5xx, timeout, or connection error."""

    @property
    def retryable(self) -> bool:
        return True


class ExchangeRejectionError(ExchangeError):
    """Bybit rejected the request. Retrying the same request will not help."""


def error_for_ret_code(
    ret_code: int,
    message: str,
    *,
    http_status: int | None = None,
    path: str | None = None,
    reset_at: float | None = None,
) -> ExchangeError:
    """Map a non-zero Bybit retCode to the matching ``ExchangeError`` subclass."""
    text = f"Bybit retCode {ret_code}: {message}" if message else f"Bybit retCode {ret_code}"
    if ret_code in AUTH_RET_CODES:
        return ExchangeAuthError(text, ret_code=ret_code, http_status=http_status, path=path)
    if ret_code in RATE_LIMIT_RET_CODES:
        return ExchangeRateLimitError(
            text, ret_code=ret_code, http_status=http_status, path=path, reset_at=reset_at
        )
    if ret_code in TEMPORARY_RET_CODES:
        return ExchangeTemporaryError(text, ret_code=ret_code, http_status=http_status, path=path)
    return ExchangeRejectionError(text, ret_code=ret_code, http_status=http_status, path=path)
