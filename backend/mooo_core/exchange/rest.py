"""Thin Bybit V5 REST client (Exchange Adapter blueprint, BybitRestClient).

Every Bybit REST call goes through this client. It signs requests with HMAC-SHA256,
blocks any path outside the endpoint allow-list, maps Bybit errors to ``ExchangeError``
subclasses, and waits for Bybit rate-limit resets. Withdrawal, transfer, sub-account,
and API key management endpoints are unreachable by construction.
"""

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlencode

import httpx
import structlog
from pydantic import SecretStr

from mooo_core.exchange.errors import (
    RATE_LIMIT_RET_CODES,
    ExchangeAuthError,
    ExchangeRateLimitError,
    ExchangeRejectionError,
    ExchangeTemporaryError,
    ForbiddenEndpointError,
    error_for_ret_code,
)
from mooo_core.exchange.rate_limit import EndpointGroup, RateLimiter, endpoint_group
from mooo_core.redaction import SecretRedactor, default_redactor

DEMO_BASE_URL = "https://api-demo.bybit.com"
LIVE_BASE_URL = "https://api.bybit.com"
BASE_URLS: Mapping[str, str] = {"demo": DEMO_BASE_URL, "live": LIVE_BASE_URL}
RECV_WINDOW_MS = 5000
SIGN_TYPE = "2"
SERVER_TIME_PATH = "/v5/market/time"
DEFAULT_TIMEOUT_S = 10.0
# Bybit answers HTTP 403 when the per-IP limit is exceeded. The ban lasts 10 minutes.
IP_BAN_S = 600.0
# Wait used when Bybit reports a rate-limit error without a reset timestamp.
DEFAULT_RATE_LIMIT_WAIT_S = 1.0

ALLOWED_PATHS = frozenset(
    {
        "/v5/account/wallet-balance",
        "/v5/account/info",
        "/v5/account/fee-rate",
        "/v5/position/list",
        "/v5/position/set-leverage",
        "/v5/position/trading-stop",
        "/v5/position/switch-mode",
        "/v5/position/closed-pnl",
        "/v5/order/create",
        "/v5/order/amend",
        "/v5/order/cancel",
        "/v5/order/cancel-all",
        "/v5/order/realtime",
        "/v5/order/history",
        "/v5/execution/list",
        "/v5/user/query-api",
    }
)
_MARKET_PATH = re.compile(r"/v5/market/[a-z0-9-]+")
_METHODS = frozenset({"GET", "POST"})


class RequestLogger(Protocol):
    def info(self, event: str, **fields: Any) -> Any: ...


_default_logger: RequestLogger = structlog.get_logger("mooo.exchange.rest")


def base_url_for(environment: object) -> str:
    """Return the REST base URL for ``demo`` or ``live``. Anything else is refused."""
    key = str(getattr(environment, "value", environment)).lower()
    try:
        return BASE_URLS[key]
    except KeyError:
        raise ValueError(f"No Bybit REST base URL for environment {key!r}") from None


def is_allowed_endpoint(path: str) -> bool:
    return path in ALLOWED_PATHS or _MARKET_PATH.fullmatch(path) is not None


def check_endpoint(path: str) -> None:
    """Raise ``ForbiddenEndpointError`` for any path outside the allow-list."""
    if not is_allowed_endpoint(path):
        message = f"Endpoint {path!r} is not on the Bybit allow-list"
        raise ForbiddenEndpointError(message, path=path)


def _param_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_query(params: Mapping[str, Any] | None) -> str:
    """Encode query parameters in the given order. The same string is signed and sent."""
    if not params:
        return ""
    pairs = [(key, _param_text(value)) for key, value in params.items() if value is not None]
    return urlencode(pairs)


def encode_body(body: Mapping[str, Any] | None) -> str:
    """Encode a JSON body compactly. The same string is signed and sent."""
    return json.dumps(dict(body or {}), separators=(",", ":"), default=str)


def _server_time_ms(result: Mapping[str, Any]) -> int:
    try:
        if result.get("timeNano") is not None:
            return int(result["timeNano"]) // 1_000_000
        return int(result["timeSecond"]) * 1000
    except (KeyError, TypeError, ValueError) as exc:
        raise ExchangeTemporaryError("Bybit server time response is malformed") from exc


class BybitRestClient:
    """Signed, allow-listed, rate-limited access to the Bybit V5 REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | SecretStr | None = None,
        recv_window_ms: int = RECV_WINDOW_MS,
        http_client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
        rate_limit_retries: int = 0,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        redactor: SecretRedactor = default_redactor,
        logger: RequestLogger | None = None,
    ) -> None:
        if base_url not in BASE_URLS.values():
            raise ValueError(f"Unknown Bybit REST base URL {base_url!r}")
        secret = api_secret.get_secret_value() if isinstance(api_secret, SecretStr) else api_secret
        self._base_url = base_url
        self._api_key = api_key or None
        self._api_secret = secret or None
        redactor.register(self._api_key, self._api_secret)
        self._redactor = redactor
        self._recv_window_ms = recv_window_ms
        self._http = http_client if http_client is not None else httpx.AsyncClient(
            timeout=timeout_s
        )
        self._owns_http = http_client is None
        self._clock = clock
        self._limiter = limiter if limiter is not None else RateLimiter(clock=clock)
        self._rate_limit_retries = max(0, rate_limit_retries)
        self._log = logger if logger is not None else _default_logger
        self._time_offset_ms = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_credentials(self) -> bool:
        return self._api_key is not None and self._api_secret is not None

    @property
    def time_offset_ms(self) -> int:
        return self._time_offset_ms

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def timestamp_ms(self) -> int:
        """Local time in epoch milliseconds, corrected by the last server time sync."""
        return int(self._clock() * 1000) + self._time_offset_ms

    def sign(self, timestamp_ms: int, payload: str) -> str:
        """HMAC-SHA256 of timestamp + api key + recv window + payload, hex encoded."""
        api_key, api_secret = self._credential()
        message = f"{timestamp_ms}{api_key}{self._recv_window_ms}{payload}"
        return hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    async def sync_time(self) -> int:
        """Read /v5/market/time and store the server minus local offset in milliseconds."""
        before = self._clock()
        result = await self.request("GET", SERVER_TIME_PATH, signed=False)
        after = self._clock()
        local_ms = int((before + after) / 2 * 1000)
        self._time_offset_ms = _server_time_ms(result) - local_ms
        return self._time_offset_ms

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, signed: bool | None = None
    ) -> dict[str, Any]:
        """GET an allow-listed path. Private paths are signed unless ``signed`` says no."""
        if signed is None:
            signed = not path.startswith("/v5/market/")
        return await self.request("GET", path, params=params, signed=signed)

    async def post(self, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """POST a signed JSON body to an allow-listed path."""
        return await self.request("POST", path, body=body, signed=True)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        signed: bool,
    ) -> dict[str, Any]:
        """Send one request and return Bybit's ``result`` object.

        The allow-list check runs first, so a forbidden path never reaches the network.
        After a rate-limit error the next request in the same group waits for the
        reset time Bybit reported; ``rate_limit_retries`` retries after that wait.
        """
        check_endpoint(path)
        verb = method.upper()
        if verb not in _METHODS:
            raise ValueError(f"Unsupported HTTP method {method!r}")
        if signed and not self.has_credentials:
            raise ExchangeAuthError("No Bybit API credential is configured", path=path)
        attempts = 0
        while True:
            try:
                return await self._send(verb, path, params=params, body=body, signed=signed)
            except ExchangeRateLimitError:
                if attempts >= self._rate_limit_retries:
                    raise
                attempts += 1

    def _credential(self) -> tuple[str, str]:
        if self._api_key is None or self._api_secret is None:
            raise ExchangeAuthError("No Bybit API credential is configured")
        return self._api_key, self._api_secret

    def _auth_headers(self, payload: str) -> dict[str, str]:
        api_key, _ = self._credential()
        timestamp = self.timestamp_ms()
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-SIGN": self.sign(timestamp, payload),
            "X-BAPI-SIGN-TYPE": SIGN_TYPE,
            "X-BAPI-RECV-WINDOW": str(self._recv_window_ms),
        }

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None,
        signed: bool,
    ) -> dict[str, Any]:
        group = endpoint_group(path)
        await self._limiter.acquire(group)
        query = encode_query(params) if method == "GET" else ""
        content = encode_body(body) if method == "POST" else ""
        headers: dict[str, str] = {}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if signed:
            headers.update(self._auth_headers(query if method == "GET" else content))
        url = f"{self._base_url}{path}?{query}" if query else f"{self._base_url}{path}"
        fields = params if method == "GET" else body
        started = self._clock()
        try:
            response = await self._http.request(
                method, url, content=content or None, headers=headers
            )
        except httpx.TimeoutException as exc:
            self._log_request(method, path, fields, None, started)
            raise ExchangeTemporaryError("Bybit request timed out", path=path) from exc
        except httpx.TransportError as exc:
            self._log_request(method, path, fields, None, started)
            raise ExchangeTemporaryError("Could not reach Bybit", path=path) from exc
        self._log_request(method, path, fields, response.status_code, started)
        reset_at = self._limiter.update_from_headers(group, response.headers)
        return self._parse(response, path, group, reset_at)

    def _log_request(
        self,
        method: str,
        path: str,
        fields: Mapping[str, Any] | None,
        http_status: int | None,
        started: float,
    ) -> None:
        # Headers are never logged. Field values pass through the secret redactor.
        self._log.info(
            "bybit_request",
            method=method,
            path=path,
            base_url=self._base_url,
            http_status=http_status,
            elapsed_ms=round((self._clock() - started) * 1000, 1),
            params=self._redactor.redact(dict(fields or {})),
        )

    def _rate_limited(
        self,
        group: EndpointGroup,
        reset_at: float | None,
        message: str,
        *,
        ret_code: int | None,
        http_status: int,
        path: str,
    ) -> ExchangeRateLimitError:
        until = reset_at if reset_at is not None else self._clock() + DEFAULT_RATE_LIMIT_WAIT_S
        self._limiter.block_until(group, until)
        return ExchangeRateLimitError(
            message, ret_code=ret_code, http_status=http_status, path=path, reset_at=until
        )

    def _parse(
        self,
        response: httpx.Response,
        path: str,
        group: EndpointGroup,
        reset_at: float | None,
    ) -> dict[str, Any]:
        status = response.status_code
        if status == 403:
            until = self._clock() + IP_BAN_S
            self._limiter.block_all_until(until)
            raise ExchangeRateLimitError(
                "Bybit IP rate limit exceeded", http_status=status, path=path, reset_at=until
            )
        if status == 401:
            raise ExchangeAuthError(
                "Bybit rejected the API credential", http_status=status, path=path
            )
        if status == 429:
            raise self._rate_limited(
                group, reset_at, "Bybit rate limit exceeded", ret_code=None, http_status=status,
                path=path,
            )
        if status >= 500:
            raise ExchangeTemporaryError(f"Bybit HTTP {status}", http_status=status, path=path)
        try:
            data = response.json()
            ret_code = int(data["retCode"])
        except (ValueError, TypeError, KeyError) as exc:
            message = f"Bybit HTTP {status} response has no retCode"
            if status >= 400:
                raise ExchangeRejectionError(message, http_status=status, path=path) from exc
            raise ExchangeTemporaryError(message, http_status=status, path=path) from exc
        if ret_code == 0:
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        ret_msg = str(data.get("retMsg") or "")
        if ret_code in RATE_LIMIT_RET_CODES:
            raise self._rate_limited(
                group, reset_at, f"Bybit retCode {ret_code}: {ret_msg}", ret_code=ret_code,
                http_status=status, path=path,
            )
        raise error_for_ret_code(ret_code, ret_msg, http_status=status, path=path)
