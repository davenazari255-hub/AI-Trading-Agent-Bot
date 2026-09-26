"""Bybit V5 REST client (Exchange Adapter blueprint, BybitRestClient, ADR-001).

Every Bybit REST call goes through this thin httpx client. It:

* refuses any method and path outside the endpoint allow-list before a request is built, so
  withdrawal, transfer, sub-account, and API key management endpoints are unreachable;
* signs private requests with HMAC-SHA256 (X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN,
  X-BAPI-RECV-WINDOW=5000). The timestamp is corrected by server time from /v5/market/time;
* maps HTTP failures and Bybit retCodes to ``ExchangeError`` subclasses;
* applies a token bucket per endpoint group, reads X-Bapi-Limit-Status and
  X-Bapi-Limit-Reset-Timestamp, and on a rate-limit error waits until the reset time Bybit
  reports before it retries;
* logs only redacted request metadata. Header values and secrets are never logged.

The client retries only rejections that Bybit did not process: rate-limit errors (after the
reported reset) and, once per call, a timestamp error (10002) after a new server time sync.
Temporary errors are raised to the caller, whose retry policy decides what to do.
"""

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from mooo_core.exchange.endpoints import EndpointSpec, resolve_endpoint
from mooo_core.exchange.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeRejectionError,
    ExchangeTemporaryError,
    error_for_ret_code,
)
from mooo_core.exchange.rate_limit import RateLimiter
from mooo_core.log import get_logger
from mooo_core.redaction import SecretRedactor, default_redactor


class BybitNetwork(StrEnum):
    DEMO = "demo"
    MAINNET = "mainnet"


BASE_URLS: Mapping[BybitNetwork, str] = {
    BybitNetwork.DEMO: "https://api-demo.bybit.com",
    BybitNetwork.MAINNET: "https://api.bybit.com",
}
RECV_WINDOW_MS = 5000
SERVER_TIME_PATH = "/v5/market/time"
TIMESTAMP_ERROR_RET_CODE = 10002

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_TIME_SYNC_INTERVAL_S = 300.0
DEFAULT_MAX_RATE_LIMIT_RETRIES = 2
DEFAULT_MAX_RATE_LIMIT_WAIT_S = 60.0
FALLBACK_RATE_LIMIT_WAIT_S = 1.0
RESET_MARGIN_S = 0.05

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Params = Mapping[str, Any]


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    """Bybit rate-limit headers of one response."""

    limit: int | None = None
    remaining: int | None = None
    reset_at_ms: int | None = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> Self:
        return cls(
            limit=_int_or_none(headers.get("X-Bapi-Limit")),
            remaining=_int_or_none(headers.get("X-Bapi-Limit-Status")),
            reset_at_ms=_int_or_none(headers.get("X-Bapi-Limit-Reset-Timestamp")),
        )


@dataclass(frozen=True, slots=True)
class BybitResponse:
    """A successful (retCode 0) Bybit V5 response."""

    result: dict[str, Any]
    ret_ext_info: dict[str, Any] = field(default_factory=dict)
    time_ms: int | None = None
    rate_limit: RateLimitStatus = field(default_factory=RateLimitStatus)


def sign_payload(secret: str, payload: str) -> str:
    """Bybit V5 HMAC-SHA256 signature as lowercase hex."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot encode {type(value).__name__} in a Bybit request body")


def encode_query(params: Params | None) -> str:
    """Query string in caller order, without None values. The same string is signed."""
    if not params:
        return ""
    return urlencode([(key, _query_value(value)) for key, value in params.items() if value is not None])


def encode_body(body: Params | None) -> str:
    """Compact JSON body without None values. The same string is signed."""
    cleaned = {key: value for key, value in (body or {}).items() if value is not None}
    return json.dumps(cleaned, separators=(",", ":"), default=_json_default)


def _as_secret(value: SecretStr | str | None) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    return SecretStr(raw) if raw else None


class BybitRestClient:
    """Signed, allow-listed, rate-limited Bybit V5 REST client."""

    def __init__(
        self,
        network: BybitNetwork | str,
        *,
        api_key: SecretStr | str | None = None,
        api_secret: SecretStr | str | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        recv_window_ms: int = RECV_WINDOW_MS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        time_sync_interval_s: float = DEFAULT_TIME_SYNC_INTERVAL_S,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
        max_rate_limit_wait_s: float = DEFAULT_MAX_RATE_LIMIT_WAIT_S,
        redactor: SecretRedactor | None = None,
        clock: Clock = time.time,
        monotonic: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._network = BybitNetwork(network)
        self._base_url = BASE_URLS[self._network]

        key = _as_secret(api_key)
        secret = _as_secret(api_secret)
        if (key is None) != (secret is None):
            raise ValueError("api_key and api_secret must be provided together")
        if recv_window_ms <= 0:
            raise ValueError("recv_window_ms must be positive")
        if max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries must not be negative")
        self._api_key = key
        self._api_secret = secret
        self._redactor = redactor or default_redactor
        if key is not None and secret is not None:
            self._redactor.register(key, secret)

        self._recv_window_ms = recv_window_ms
        self._time_sync_interval_s = time_sync_interval_s
        self._max_rate_limit_retries = max_rate_limit_retries
        self._max_rate_limit_wait_s = max_rate_limit_wait_s
        self._clock = clock
        self._monotonic = monotonic
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), follow_redirects=False
        )
        self._limiter = rate_limiter or RateLimiter(clock=monotonic, sleep=sleep)
        self._offset_ms = 0.0
        self._last_sync_at: float | None = None
        self._sync_lock = asyncio.Lock()
        self._log = get_logger(__name__)

    def __repr__(self) -> str:
        credentials = "set" if self.has_credentials else "none"
        return f"BybitRestClient(network={self._network.value}, credentials={credentials})"

    @property
    def network(self) -> BybitNetwork:
        return self._network

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_credentials(self) -> bool:
        return self._api_key is not None and self._api_secret is not None

    @property
    def server_time_offset_ms(self) -> float:
        return self._offset_ms

    def server_time_ms(self) -> int:
        """Local time corrected by the last server time sync, in milliseconds."""
        return int(self._clock() * 1000 + self._offset_ms)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def sync_time(self) -> int:
        """Read Bybit server time and store the offset used for signed timestamps."""
        started = self._clock()
        response = await self._execute(resolve_endpoint("GET", SERVER_TIME_PATH), None)
        finished = self._clock()
        server_ms = _server_time_ms(response)
        self._offset_ms = server_ms - (started + finished) / 2 * 1000
        self._last_sync_at = self._monotonic()
        self._log.info(
            "bybit_time_synced", network=self._network.value, offset_ms=round(self._offset_ms)
        )
        return server_ms

    async def get(self, path: str, params: Params | None = None) -> BybitResponse:
        return await self.request("GET", path, params)

    async def post(self, path: str, body: Params | None = None) -> BybitResponse:
        return await self.request("POST", path, body)

    async def request(self, method: str, path: str, params: Params | None = None) -> BybitResponse:
        """Send one allow-listed request. Raises ``ExchangeError`` subclasses on failure."""
        spec = resolve_endpoint(method, path)
        if spec.signed:
            if not self.has_credentials:
                raise ExchangeAuthError(
                    "Bybit credentials are required for this endpoint", path=spec.path
                )
            await self._ensure_time_synced()
        return await self._execute(spec, params)

    def _sync_due(self) -> bool:
        return (
            self._last_sync_at is None
            or self._monotonic() - self._last_sync_at >= self._time_sync_interval_s
        )

    async def _ensure_time_synced(self) -> None:
        if not self._sync_due():
            return
        async with self._sync_lock:
            if self._sync_due():
                await self.sync_time()

    def _reset_delay_s(self, reset_at_ms: int | None) -> float:
        if reset_at_ms is None:
            return FALLBACK_RATE_LIMIT_WAIT_S
        return max(0.0, (reset_at_ms - self.server_time_ms()) / 1000) + RESET_MARGIN_S

    async def _execute(self, spec: EndpointSpec, params: Params | None) -> BybitResponse:
        rate_limit_retries = 0
        resynced = False
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire(spec.group)
            try:
                return await self._send_once(spec, params, attempt)
            except ExchangeRateLimitError as exc:
                delay = self._reset_delay_s(exc.reset_at_ms)
                if (
                    rate_limit_retries >= self._max_rate_limit_retries
                    or delay > self._max_rate_limit_wait_s
                ):
                    raise
                rate_limit_retries += 1
                if exc.ip_wide:
                    self._limiter.block_all(delay)
                else:
                    self._limiter.block_group(spec.group, delay)
                self._log.warning(
                    "bybit_rate_limited",
                    path=spec.path,
                    ret_code=exc.ret_code,
                    http_status=exc.http_status,
                    reset_at_ms=exc.reset_at_ms,
                    wait_s=round(delay, 3),
                    retry=rate_limit_retries,
                )
            except ExchangeRejectionError as exc:
                if exc.ret_code == TIMESTAMP_ERROR_RET_CODE and spec.signed and not resynced:
                    resynced = True
                    self._log.warning("bybit_timestamp_rejected", path=spec.path)
                    await self.sync_time()
                    continue
                raise

    def _auth_headers(self, payload: str) -> dict[str, str]:
        if self._api_key is None or self._api_secret is None:
            raise ExchangeAuthError("Bybit credentials are required for this endpoint")
        timestamp = str(self.server_time_ms())
        recv_window = str(self._recv_window_ms)
        api_key = self._api_key.get_secret_value()
        signature = sign_payload(
            self._api_secret.get_secret_value(), timestamp + api_key + recv_window + payload
        )
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    async def _send_once(
        self, spec: EndpointSpec, params: Params | None, attempt: int
    ) -> BybitResponse:
        url = self._base_url + spec.path
        headers = {"Accept": "application/json"}
        content: bytes | None = None
        if spec.method == "GET":
            payload = encode_query(params)
            if payload:
                url = f"{url}?{payload}"
        else:
            payload = encode_body(params)
            content = payload.encode("utf-8")
            headers["Content-Type"] = "application/json"
        if spec.signed:
            headers.update(self._auth_headers(payload))

        started = self._monotonic()
        try:
            response = await self._http.request(spec.method, url, content=content, headers=headers)
        except httpx.TimeoutException as exc:
            self._log_request(spec, params, attempt=attempt, started=started, outcome="timeout")
            raise ExchangeTemporaryError("Bybit request timed out", path=spec.path) from exc
        except httpx.HTTPError as exc:
            self._log_request(
                spec, params, attempt=attempt, started=started, outcome="transport_error"
            )
            raise ExchangeTemporaryError(
                f"Bybit transport error: {type(exc).__name__}", path=spec.path
            ) from exc

        status = RateLimitStatus.from_headers(response.headers)
        self._observe_rate_limit(spec, status)
        try:
            parsed = self._parse(spec, response, status)
        except ExchangeError as exc:
            self._log_request(
                spec,
                params,
                attempt=attempt,
                started=started,
                outcome=type(exc).__name__,
                http_status=response.status_code,
                ret_code=exc.ret_code,
                rate_limit=status,
            )
            raise
        self._log_request(
            spec,
            params,
            attempt=attempt,
            started=started,
            outcome="ok",
            http_status=response.status_code,
            ret_code=0,
            rate_limit=status,
        )
        return parsed

    def _observe_rate_limit(self, spec: EndpointSpec, status: RateLimitStatus) -> None:
        """Stop sending to a group whose quota Bybit reports as used up until its reset."""
        if status.remaining is None or status.remaining > 0 or status.reset_at_ms is None:
            return
        delay = min(self._reset_delay_s(status.reset_at_ms), self._max_rate_limit_wait_s)
        self._limiter.block_group(spec.group, delay)

    def _parse(
        self, spec: EndpointSpec, response: httpx.Response, status: RateLimitStatus
    ) -> BybitResponse:
        code = response.status_code
        if code >= 500:
            raise ExchangeTemporaryError(f"Bybit HTTP {code}", http_status=code, path=spec.path)
        if code in (403, 429):
            # 403 "access too frequent" is the HTTP IP limit.
            raise ExchangeRateLimitError(
                "Bybit HTTP rate limit",
                http_status=code,
                path=spec.path,
                reset_at_ms=status.reset_at_ms,
                ip_wide=code == 403,
            )
        if code == 401:
            raise ExchangeAuthError("Bybit rejected the credentials", http_status=code, path=spec.path)

        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or _int_or_none(data.get("retCode")) is None:
            if code != 200:
                raise ExchangeRejectionError(f"Bybit HTTP {code}", http_status=code, path=spec.path)
            raise ExchangeTemporaryError(
                "Bybit returned an unreadable response", http_status=code, path=spec.path
            )

        ret_code = _int_or_none(data.get("retCode")) or 0
        if ret_code != 0:
            message = self._redactor.redact_text(str(data.get("retMsg") or "Bybit error"))
            raise error_for_ret_code(
                ret_code,
                message,
                http_status=code,
                path=spec.path,
                reset_at_ms=status.reset_at_ms,
            )
        if code != 200:
            raise ExchangeRejectionError(f"Bybit HTTP {code}", http_status=code, path=spec.path)

        result = data.get("result")
        ret_ext_info = data.get("retExtInfo")
        return BybitResponse(
            result=result if isinstance(result, dict) else {},
            ret_ext_info=ret_ext_info if isinstance(ret_ext_info, dict) else {},
            time_ms=_int_or_none(data.get("time")),
            rate_limit=status,
        )

    def _log_request(
        self,
        spec: EndpointSpec,
        params: Params | None,
        *,
        attempt: int,
        started: float,
        outcome: str,
        http_status: int | None = None,
        ret_code: int | None = None,
        rate_limit: RateLimitStatus | None = None,
    ) -> None:
        """Log request metadata. Header values are never logged; params are redacted."""
        emit = self._log.debug if outcome == "ok" else self._log.warning
        emit(
            "bybit_request",
            network=self._network.value,
            method=spec.method,
            path=spec.path,
            group=spec.group.value,
            signed=spec.signed,
            params=self._redactor.redact(dict(params)) if params else None,
            attempt=attempt,
            outcome=outcome,
            http_status=http_status,
            ret_code=ret_code,
            limit_remaining=rate_limit.remaining if rate_limit else None,
            elapsed_ms=round((self._monotonic() - started) * 1000, 1),
        )


def _server_time_ms(response: BybitResponse) -> int:
    nano = _int_or_none(response.result.get("timeNano"))
    if nano is not None:
        return nano // 1_000_000
    seconds = _int_or_none(response.result.get("timeSecond"))
    if seconds is not None:
        return seconds * 1000
    if response.time_ms is not None:
        return response.time_ms
    raise ExchangeTemporaryError("Bybit server time response has no time", path=SERVER_TIME_PATH)
