"""Bybit V5 REST client tests (WO-5). Recorded responses follow the Bybit V5 docs.

Covers AC-MD-002.3 (client-side rate limits) and AC-EXEC-003.2 (wait for the reported
reset time before retrying after a rate-limit error).
"""

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from mooo_core.exchange.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeRejectionError,
    ExchangeTemporaryError,
    ForbiddenEndpointError,
)
from mooo_core.exchange.rate_limit import (
    EndpointGroup,
    RateLimiter,
    TokenBucket,
    endpoint_group,
    parse_reset_timestamp,
)
from mooo_core.exchange.rest import (
    ALLOWED_PATHS,
    DEMO_BASE_URL,
    IP_BAN_S,
    LIVE_BASE_URL,
    BybitRestClient,
    base_url_for,
    is_allowed_endpoint,
)

API_KEY = "unit-test-api-key-001"
API_SECRET = "unit-test-api-secret-0123456789abcdef"
NOW = 1_700_000_000.0


class FakeTime:
    def __init__(self, now: float = NOW) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """MockTransport handler that replays responses in order and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class ListLogger:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.entries.append({"event": event, **fields})


def ok(result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": result or {},
        "retExtInfo": {},
        "time": 1700000000000,
    }


def failure(ret_code: int, message: str) -> dict[str, Any]:
    return {"retCode": ret_code, "retMsg": message, "result": {}, "retExtInfo": {}}


def respond(
    payload: dict[str, Any], *, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def reset_header(at: float, remaining: int | None = None) -> dict[str, str]:
    headers = {"X-Bapi-Limit-Reset-Timestamp": str(int(at * 1000)), "X-Bapi-Limit": "10"}
    if remaining is not None:
        headers["X-Bapi-Limit-Status"] = str(remaining)
    return headers


def make_client(
    recorder: Recorder,
    fake: FakeTime,
    *,
    credentials: bool = True,
    rate_limit_retries: int = 0,
    logger: ListLogger | None = None,
) -> BybitRestClient:
    return BybitRestClient(
        base_url=DEMO_BASE_URL,
        api_key=API_KEY if credentials else None,
        api_secret=API_SECRET if credentials else None,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        limiter=RateLimiter(clock=fake.clock, sleep=fake.sleep),
        clock=fake.clock,
        rate_limit_retries=rate_limit_retries,
        logger=logger or ListLogger(),
    )


def expected_sign(timestamp: str, payload: str) -> str:
    message = f"{timestamp}{API_KEY}5000{payload}".encode()
    return hmac.new(API_SECRET.encode(), message, hashlib.sha256).hexdigest()


async def test_signed_get_follows_bybit_v5_signing() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok({"category": "linear", "list": []})))
    client = make_client(recorder, fake)
    result = await client.get("/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})
    assert result == {"category": "linear", "list": []}
    request = recorder.requests[0]
    query = "category=linear&symbol=BTCUSDT"
    assert request.method == "GET"
    assert str(request.url) == f"{DEMO_BASE_URL}/v5/position/list?{query}"
    assert request.headers["X-BAPI-API-KEY"] == API_KEY
    assert request.headers["X-BAPI-TIMESTAMP"] == "1700000000000"
    assert request.headers["X-BAPI-RECV-WINDOW"] == "5000"
    assert request.headers["X-BAPI-SIGN-TYPE"] == "2"
    assert request.headers["X-BAPI-SIGN"] == expected_sign("1700000000000", query)


async def test_signed_post_signs_the_exact_json_body() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok({"orderId": "1", "orderLinkId": "mooo-1"})))
    client = make_client(recorder, fake)
    body = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.001",
        "positionIdx": 0,
        "orderLinkId": "mooo-1",
    }
    await client.post("/v5/order/create", body)
    request = recorder.requests[0]
    sent = request.content.decode()
    assert json.loads(sent) == body
    assert " " not in sent
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-BAPI-SIGN"] == expected_sign("1700000000000", sent)


async def test_market_get_is_unsigned_and_needs_no_credential() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok({"category": "linear", "list": []})))
    client = make_client(recorder, fake, credentials=False)
    await client.get("/v5/market/tickers", {"category": "linear"})
    assert "X-BAPI-SIGN" not in recorder.requests[0].headers
    assert "X-BAPI-API-KEY" not in recorder.requests[0].headers


async def test_signed_call_without_credential_is_refused_before_sending() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok()))
    client = make_client(recorder, fake, credentials=False)
    with pytest.raises(ExchangeAuthError):
        await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    assert recorder.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/v5/asset/withdraw/create",
        "/v5/asset/transfer/inter-transfer",
        "/v5/asset/transfer/universal-transfer",
        "/v5/user/create-sub-member",
        "/v5/user/create-sub-api",
        "/v5/user/update-api",
        "/v5/user/delete-api",
        "/v5/order/create-batch",
        "/v5/market/../asset/withdraw/create",
        "/v5/market/tickers?category=linear",
        "https://api.bybit.com/v5/market/tickers",
        "/v5/market/",
        "",
    ],
)
async def test_paths_outside_the_allow_list_never_reach_the_network(path: str) -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok()))
    client = make_client(recorder, fake)
    with pytest.raises(ForbiddenEndpointError):
        await client.post(path, {"coin": "USDT", "amount": "1"})
    with pytest.raises(ForbiddenEndpointError):
        await client.request("GET", path, signed=False)
    assert recorder.requests == []


def test_allow_list_matches_the_exchange_adapter_blueprint() -> None:
    assert ALLOWED_PATHS == {
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
    for market_path in ("/v5/market/time", "/v5/market/kline", "/v5/market/open-interest"):
        assert is_allowed_endpoint(market_path)


@pytest.mark.parametrize(
    ("ret_code", "error_type", "retryable"),
    [
        (10003, ExchangeAuthError, False),
        (10004, ExchangeAuthError, False),
        (10005, ExchangeAuthError, False),
        (33004, ExchangeAuthError, False),
        (10006, ExchangeRateLimitError, True),
        (10018, ExchangeRateLimitError, True),
        (10000, ExchangeTemporaryError, True),
        (10016, ExchangeTemporaryError, True),
        (10001, ExchangeRejectionError, False),
        (110007, ExchangeRejectionError, False),
    ],
)
async def test_ret_codes_map_to_exchange_errors(
    ret_code: int, error_type: type[ExchangeError], retryable: bool
) -> None:
    fake = FakeTime()
    recorder = Recorder(respond(failure(ret_code, "recorded error")))
    client = make_client(recorder, fake)
    with pytest.raises(error_type) as caught:
        await client.post("/v5/order/cancel", {"category": "linear", "orderLinkId": "x"})
    assert type(caught.value) is error_type
    assert caught.value.ret_code == ret_code
    assert caught.value.retryable is retryable
    assert caught.value.path == "/v5/order/cancel"


async def test_http_5xx_timeouts_and_connection_errors_are_temporary() -> None:
    fake = FakeTime()
    for failure_item in (
        httpx.Response(502, text="Bad Gateway"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ):
        client = make_client(Recorder(failure_item), fake)
        with pytest.raises(ExchangeTemporaryError):
            await client.get("/v5/market/tickers", {"category": "linear"})


async def test_http_403_blocks_every_group_for_the_ip_ban() -> None:
    fake = FakeTime()
    client = make_client(Recorder(httpx.Response(403, text="access too frequent")), fake)
    with pytest.raises(ExchangeRateLimitError) as caught:
        await client.get("/v5/market/tickers", {"category": "linear"})
    assert caught.value.reset_at == pytest.approx(NOW + IP_BAN_S)
    for group in EndpointGroup:
        assert client.limiter.blocked_until(group) == pytest.approx(NOW + IP_BAN_S)


async def test_rate_limit_error_waits_for_reported_reset_before_retrying() -> None:
    fake = FakeTime()
    recorder = Recorder(
        respond(failure(10006, "Too many visits!"), headers=reset_header(NOW + 2.5, 0)),
        respond(ok({"orderId": "1", "orderLinkId": "mooo-2"})),
    )
    client = make_client(recorder, fake, rate_limit_retries=1)
    result = await client.post("/v5/order/create", {"orderLinkId": "mooo-2"})
    assert result["orderLinkId"] == "mooo-2"
    assert len(recorder.requests) == 2
    assert fake.sleeps == [pytest.approx(2.5)]
    retry_timestamp = int(recorder.requests[1].headers["X-BAPI-TIMESTAMP"])
    assert retry_timestamp >= int((NOW + 2.5) * 1000)


async def test_rate_limit_error_blocks_only_its_group_until_reset() -> None:
    fake = FakeTime()
    recorder = Recorder(
        respond(failure(10006, "Too many visits!"), headers=reset_header(NOW + 3.0)),
        respond(ok({"list": []})),
        respond(ok({"orderId": "2"})),
    )
    client = make_client(recorder, fake)
    with pytest.raises(ExchangeRateLimitError) as caught:
        await client.post("/v5/order/create", {"orderLinkId": "mooo-3"})
    assert caught.value.reset_at == pytest.approx(NOW + 3.0)
    await client.get("/v5/market/tickers", {"category": "linear"})
    assert fake.sleeps == []
    await client.post("/v5/order/create", {"orderLinkId": "mooo-3"})
    assert fake.sleeps == [pytest.approx(3.0)]


async def test_rate_limit_error_without_reset_header_waits_a_default_second() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(failure(10018, "Out of IP limit")), respond(ok()))
    client = make_client(recorder, fake, rate_limit_retries=1)
    await client.get("/v5/order/realtime", {"category": "linear"})
    assert fake.sleeps == [pytest.approx(1.0)]


async def test_exhausted_limit_status_blocks_the_group_until_reset() -> None:
    fake = FakeTime()
    recorder = Recorder(
        respond(ok({"list": []}), headers=reset_header(NOW + 1.0, 0)),
        respond(ok({"list": []})),
    )
    client = make_client(recorder, fake)
    await client.get("/v5/position/list", {"category": "linear"})
    assert fake.sleeps == []
    await client.get("/v5/position/list", {"category": "linear"})
    assert fake.sleeps == [pytest.approx(1.0)]


async def test_remaining_requests_do_not_block() -> None:
    fake = FakeTime()
    recorder = Recorder(respond(ok({"list": []}), headers=reset_header(NOW + 1.0, 7)))
    client = make_client(recorder, fake)
    await client.get("/v5/position/list", {"category": "linear"})
    await client.get("/v5/position/list", {"category": "linear"})
    assert fake.sleeps == []


def test_token_bucket_spaces_requests_after_the_burst() -> None:
    fake = FakeTime()
    bucket = TokenBucket(1.0, 2, clock=fake.clock)
    assert [bucket.reserve() for _ in range(4)] == [0.0, 0.0, 1.0, 2.0]
    fake.now += 10
    assert bucket.reserve() == 0.0


async def test_limiter_keeps_market_requests_under_the_configured_rate() -> None:
    fake = FakeTime()
    limiter = RateLimiter({EndpointGroup.MARKET: (5.0, 5)}, clock=fake.clock, sleep=fake.sleep)
    for _ in range(15):
        await limiter.acquire(EndpointGroup.MARKET)
    elapsed = fake.now - NOW
    assert elapsed == pytest.approx(2.0)
    assert 15 / elapsed <= 5.0 + 5 / elapsed


def test_endpoint_groups() -> None:
    assert endpoint_group("/v5/market/kline") is EndpointGroup.MARKET
    assert endpoint_group("/v5/order/create") is EndpointGroup.TRADE
    assert endpoint_group("/v5/order/cancel-all") is EndpointGroup.TRADE
    assert endpoint_group("/v5/order/realtime") is EndpointGroup.ORDER_QUERY
    assert endpoint_group("/v5/execution/list") is EndpointGroup.ORDER_QUERY
    assert endpoint_group("/v5/position/set-leverage") is EndpointGroup.POSITION
    assert endpoint_group("/v5/account/wallet-balance") is EndpointGroup.ACCOUNT
    assert endpoint_group("/v5/user/query-api") is EndpointGroup.ACCOUNT


def test_parse_reset_timestamp() -> None:
    assert parse_reset_timestamp("1700000001500") == pytest.approx(1_700_000_001.5)
    assert parse_reset_timestamp(None) is None
    assert parse_reset_timestamp("not-a-number") is None


async def test_sync_time_applies_the_server_offset_to_signatures() -> None:
    fake = FakeTime()
    server_time = {"timeSecond": "1700000003", "timeNano": "1700000003250000000"}
    recorder = Recorder(respond(ok(server_time)), respond(ok({"list": []})))
    client = make_client(recorder, fake)
    assert await client.sync_time() == 3250
    assert recorder.requests[0].url.path == "/v5/market/time"
    assert "X-BAPI-SIGN" not in recorder.requests[0].headers
    await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    assert recorder.requests[1].headers["X-BAPI-TIMESTAMP"] == "1700000003250"


def test_base_urls_for_demo_and_live_only() -> None:
    assert base_url_for("demo") == DEMO_BASE_URL == "https://api-demo.bybit.com"
    assert base_url_for("LIVE") == LIVE_BASE_URL == "https://api.bybit.com"
    with pytest.raises(ValueError):
        base_url_for("testnet")
    with pytest.raises(ValueError):
        BybitRestClient(base_url="https://example.com")


async def test_request_logs_hold_no_headers_or_secrets() -> None:
    fake = FakeTime()
    logger = ListLogger()
    recorder = Recorder(respond(ok({"orderId": "1"})))
    client = make_client(recorder, fake, logger=logger)
    await client.post("/v5/order/create", {"symbol": "BTCUSDT", "api_secret": API_SECRET})
    signature = recorder.requests[0].headers["X-BAPI-SIGN"]
    assert len(logger.entries) == 1
    entry = logger.entries[0]
    assert entry["event"] == "bybit_request"
    assert entry["path"] == "/v5/order/create"
    assert entry["http_status"] == 200
    assert entry["params"]["symbol"] == "BTCUSDT"
    text = repr(logger.entries)
    for secret in (API_SECRET, API_KEY, signature, "X-BAPI"):
        assert secret not in text
