"""Unit tests for BybitRestClient with recorded Bybit V5 responses (WO-5)."""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from mooo_core.exchange import (
    PRIVATE_ENDPOINTS,
    BybitNetwork,
    BybitRestClient,
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeRejectionError,
    ExchangeTemporaryError,
    ForbiddenEndpointError,
    is_allowed_endpoint,
)

FIXTURES: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "bybit_responses.json").read_text()
)
API_KEY = "unit-test-api-key-8f2c"
API_SECRET = "unit-test-api-secret-41d9e7"
START = 1_700_000_000.0


class FakeClock:
    """Wall and monotonic clock in one. ``sleep`` advances time and records the delay."""

    def __init__(self, start: float = START) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeBybit:
    """MockTransport handler: serves server time and replays queued responses."""

    def __init__(self, clock: FakeClock, *, server_offset_ms: int = 0) -> None:
        self.clock = clock
        self.server_offset_ms = server_offset_ms
        self.requests: list[httpx.Request] = []
        self.sent_at: list[float] = []
        self.queue: list[httpx.Response | Exception] = []

    def enqueue(self, item: httpx.Response | Exception) -> None:
        self.queue.append(item)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.sent_at.append(self.clock.now)
        if request.url.path == "/v5/market/time":
            ms = int(self.clock.now * 1000) + self.server_offset_ms
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {"timeSecond": str(ms // 1000), "timeNano": str(ms * 1_000_000)},
                    "retExtInfo": {},
                    "time": ms,
                },
            )
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/v5/market/time"]

    def api_sent_at(self) -> list[float]:
        return [
            at
            for at, r in zip(self.sent_at, self.requests, strict=True)
            if r.url.path != "/v5/market/time"
        ]


def recorded(
    name: str, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=FIXTURES[name], headers=headers)


def ret_code_response(ret_code: int, message: str = "error") -> httpx.Response:
    return httpx.Response(
        200,
        json={"retCode": ret_code, "retMsg": message, "result": {}, "retExtInfo": {}, "time": 1},
    )


def make_client(clock: FakeClock, bybit: FakeBybit, **kwargs: Any) -> BybitRestClient:
    kwargs.setdefault("api_key", API_KEY)
    kwargs.setdefault("api_secret", API_SECRET)
    network = kwargs.pop("network", BybitNetwork.DEMO)
    return BybitRestClient(
        network,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(bybit.handler)),
        clock=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,
    )


def expected_signature(timestamp: str, payload: str) -> str:
    message = (timestamp + API_KEY + "5000" + payload).encode()
    return hmac.new(API_SECRET.encode(), message, hashlib.sha256).hexdigest()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bybit(clock: FakeClock) -> FakeBybit:
    return FakeBybit(clock)


@pytest.fixture
async def client(clock: FakeClock, bybit: FakeBybit) -> AsyncIterator[BybitRestClient]:
    rest = make_client(clock, bybit)
    yield rest
    await rest.aclose()


async def test_demo_and_mainnet_base_urls() -> None:
    demo = BybitRestClient("demo")
    mainnet = BybitRestClient(BybitNetwork.MAINNET)
    try:
        assert demo.base_url == "https://api-demo.bybit.com"
        assert mainnet.base_url == "https://api.bybit.com"
    finally:
        await demo.aclose()
        await mainnet.aclose()
    with pytest.raises(ValueError):
        BybitRestClient("testnet")


def test_key_and_secret_must_be_provided_together() -> None:
    with pytest.raises(ValueError):
        BybitRestClient("demo", api_key=API_KEY)


async def test_signed_get_signs_exact_query_string(
    client: BybitRestClient, bybit: FakeBybit
) -> None:
    bybit.enqueue(recorded("wallet_balance"))

    response = await client.get(
        "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT", "x": None}
    )

    assert response.result["list"][0]["accountType"] == "UNIFIED"
    assert response.time_ms == 1700000000123
    request = bybit.api_requests[-1]
    assert request.url.host == "api-demo.bybit.com"
    query = request.url.query.decode()
    assert query == "accountType=UNIFIED&coin=USDT"
    headers = request.headers
    assert headers["X-BAPI-API-KEY"] == API_KEY
    assert headers["X-BAPI-RECV-WINDOW"] == "5000"
    assert headers["X-BAPI-SIGN"] == expected_signature(headers["X-BAPI-TIMESTAMP"], query)


async def test_signed_post_signs_exact_json_body(client: BybitRestClient, bybit: FakeBybit) -> None:
    bybit.enqueue(recorded("order_create"))
    body = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": Decimal("0.001"),
        "positionIdx": 0,
        "reduceOnly": False,
        "orderLinkId": "mooo-test-1",
        "price": None,
    }

    response = await client.post("/v5/order/create", body)

    assert response.result["orderLinkId"] == "mooo-test-1"
    request = bybit.api_requests[-1]
    sent = request.content.decode()
    assert json.loads(sent) == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.001",
        "positionIdx": 0,
        "reduceOnly": False,
        "orderLinkId": "mooo-test-1",
    }
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-BAPI-SIGN"] == expected_signature(
        request.headers["X-BAPI-TIMESTAMP"], sent
    )


async def test_signed_timestamp_uses_synced_server_time(clock: FakeClock) -> None:
    bybit = FakeBybit(clock, server_offset_ms=5_000)
    client = make_client(clock, bybit)
    bybit.enqueue(recorded("wallet_balance"))
    bybit.enqueue(recorded("wallet_balance"))

    await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    assert bybit.paths == ["/v5/market/time", "/v5/account/wallet-balance"]
    timestamp = int(bybit.api_requests[-1].headers["X-BAPI-TIMESTAMP"])
    assert timestamp == int(clock.now * 1000) + 5_000

    clock.now += 301
    await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    assert bybit.paths.count("/v5/market/time") == 2


async def test_public_market_request_is_not_signed(
    client: BybitRestClient, bybit: FakeBybit
) -> None:
    bybit.enqueue(recorded("tickers"))

    response = await client.get("/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"})

    assert response.result["list"][0]["symbol"] == "BTCUSDT"
    assert bybit.paths == ["/v5/market/tickers"]
    assert "X-BAPI-SIGN" not in bybit.requests[0].headers
    assert "X-BAPI-API-KEY" not in bybit.requests[0].headers


async def test_signed_endpoint_without_credentials_is_refused(
    clock: FakeClock, bybit: FakeBybit
) -> None:
    client = make_client(clock, bybit, api_key=None, api_secret=None)

    with pytest.raises(ExchangeAuthError):
        await client.get("/v5/position/list", {"category": "linear"})

    assert bybit.requests == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v5/asset/withdraw/create"),
        ("POST", "/v5/asset/transfer/inter-transfer"),
        ("POST", "/v5/asset/transfer/universal-transfer"),
        ("POST", "/v5/user/create-sub-member"),
        ("POST", "/v5/user/create-sub-api"),
        ("POST", "/v5/user/update-api"),
        ("POST", "/v5/user/delete-api"),
        ("GET", "/v5/market/../asset/withdraw/query-record"),
        ("GET", "/v5/market/tickers?category=linear"),
        ("GET", "/v5/market/%2e%2e/asset/coin/query-info"),
        ("GET", "https://evil.example/v5/market/time"),
        ("POST", "/V5/ORDER/CREATE"),
        ("POST", "/v5/order/create/"),
        ("GET", "/v5/position/list "),
        ("GET", "/v5/order/create"),
        ("POST", "/v5/market/tickers"),
        ("DELETE", "/v5/order/cancel"),
        ("GET", ""),
    ],
)
async def test_forbidden_endpoints_are_never_sent(
    client: BybitRestClient, bybit: FakeBybit, method: str, path: str
) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await client.request(method, path)
    assert bybit.requests == []


def test_blueprint_allow_list_is_reachable() -> None:
    for path, spec in PRIVATE_ENDPOINTS.items():
        assert is_allowed_endpoint(spec.method, path)
    for path in (
        "/v5/market/time",
        "/v5/market/kline",
        "/v5/market/instruments-info",
        "/v5/market/funding/history",
        "/v5/market/open-interest",
    ):
        assert is_allowed_endpoint("GET", path)
    assert not any("/asset/" in path or "withdraw" in path for path in PRIVATE_ENDPOINTS)


@pytest.mark.parametrize(
    ("ret_code", "error_type"),
    [
        (10003, ExchangeAuthError),
        (10004, ExchangeAuthError),
        (10005, ExchangeAuthError),
        (33004, ExchangeAuthError),
        (10006, ExchangeRateLimitError),
        (10018, ExchangeRateLimitError),
        (10000, ExchangeTemporaryError),
        (10016, ExchangeTemporaryError),
        (110007, ExchangeRejectionError),
        (10001, ExchangeRejectionError),
    ],
)
async def test_ret_code_mapping(
    clock: FakeClock, bybit: FakeBybit, ret_code: int, error_type: type[ExchangeError]
) -> None:
    client = make_client(clock, bybit, max_rate_limit_retries=0)
    bybit.enqueue(ret_code_response(ret_code))

    with pytest.raises(error_type) as info:
        await client.get("/v5/order/realtime", {"category": "linear"})

    assert type(info.value) is error_type
    assert info.value.ret_code == ret_code


@pytest.mark.parametrize(
    ("fixture", "error_type"),
    [
        ("error_invalid_api_key", ExchangeAuthError),
        ("error_server", ExchangeTemporaryError),
        ("error_insufficient_balance", ExchangeRejectionError),
    ],
)
async def test_recorded_error_responses(
    client: BybitRestClient, bybit: FakeBybit, fixture: str, error_type: type[ExchangeError]
) -> None:
    bybit.enqueue(recorded(fixture))
    with pytest.raises(error_type):
        await client.post("/v5/order/create", {"category": "linear", "orderLinkId": "a"})


async def test_http_failures_map_to_exchange_errors(clock: FakeClock, bybit: FakeBybit) -> None:
    client = make_client(clock, bybit, max_rate_limit_retries=0)
    bybit.enqueue(httpx.Response(502, text="<html>Bad gateway</html>"))
    bybit.enqueue(httpx.ReadTimeout("timed out"))
    bybit.enqueue(httpx.ConnectError("connection refused"))
    bybit.enqueue(httpx.Response(403, text="access too frequent"))
    bybit.enqueue(httpx.Response(200, text="not json"))

    for error_type in (
        ExchangeTemporaryError,
        ExchangeTemporaryError,
        ExchangeTemporaryError,
        ExchangeRateLimitError,
        ExchangeTemporaryError,
    ):
        with pytest.raises(error_type):
            await client.get("/v5/market/tickers", {"category": "linear"})


async def test_rate_limit_error_waits_for_reported_reset_then_retries(
    clock: FakeClock, bybit: FakeBybit, client: BybitRestClient
) -> None:
    reset_ms = int(clock.now * 1000) + 1_500
    bybit.enqueue(
        recorded(
            "error_rate_limit",
            headers={
                "X-Bapi-Limit": "10",
                "X-Bapi-Limit-Status": "0",
                "X-Bapi-Limit-Reset-Timestamp": str(reset_ms),
            },
        )
    )
    bybit.enqueue(recorded("order_create"))

    response = await client.post(
        "/v5/order/create", {"category": "linear", "orderLinkId": "mooo-test-1"}
    )

    assert response.result["orderLinkId"] == "mooo-test-1"
    first, second = bybit.api_sent_at()
    assert first * 1000 < reset_ms
    assert second * 1000 >= reset_ms
    assert sum(clock.sleeps) == pytest.approx(1.55)


async def test_rate_limit_reset_beyond_max_wait_is_raised(
    clock: FakeClock, bybit: FakeBybit
) -> None:
    client = make_client(clock, bybit, max_rate_limit_wait_s=60)
    reset_ms = int(clock.now * 1000) + 600_000
    bybit.enqueue(
        recorded("error_rate_limit", headers={"X-Bapi-Limit-Reset-Timestamp": str(reset_ms)})
    )

    with pytest.raises(ExchangeRateLimitError) as info:
        await client.get("/v5/execution/list", {"category": "linear"})

    assert info.value.reset_at_ms == reset_ms
    assert len(bybit.api_requests) == 1
    assert clock.sleeps == []


async def test_rate_limit_retries_are_bounded(clock: FakeClock, bybit: FakeBybit) -> None:
    client = make_client(clock, bybit, max_rate_limit_retries=2)
    for _ in range(3):
        bybit.enqueue(recorded("error_rate_limit"))

    with pytest.raises(ExchangeRateLimitError):
        await client.get("/v5/order/history", {"category": "linear"})

    assert len(bybit.api_requests) == 3


async def test_exhausted_quota_blocks_group_until_reset(
    clock: FakeClock, bybit: FakeBybit, client: BybitRestClient
) -> None:
    reset_ms = int(clock.now * 1000) + 2_000
    bybit.enqueue(
        recorded(
            "positions",
            headers={"X-Bapi-Limit-Status": "0", "X-Bapi-Limit-Reset-Timestamp": str(reset_ms)},
        )
    )
    bybit.enqueue(recorded("positions"))

    first = await client.get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    await client.get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})

    assert first.rate_limit.remaining == 0
    assert first.rate_limit.reset_at_ms == reset_ms
    assert bybit.api_sent_at()[1] * 1000 >= reset_ms


async def test_timestamp_error_resyncs_once_and_retries(
    client: BybitRestClient, bybit: FakeBybit
) -> None:
    bybit.enqueue(recorded("error_timestamp"))
    bybit.enqueue(recorded("wallet_balance"))

    await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    assert bybit.paths == [
        "/v5/market/time",
        "/v5/account/wallet-balance",
        "/v5/market/time",
        "/v5/account/wallet-balance",
    ]


async def test_logs_errors_and_repr_never_contain_secrets(
    clock: FakeClock, bybit: FakeBybit
) -> None:
    client = make_client(clock, bybit)
    bybit.enqueue(recorded("wallet_balance"))
    bybit.enqueue(
        ret_code_response(
            10004,
            f"error sign! origin_string[1700000000000{API_KEY}5000accountType=UNIFIED]",
        )
    )

    with capture_logs() as logs:
        await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        with pytest.raises(ExchangeAuthError) as info:
            await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})

    assert any(entry.get("outcome") == "ExchangeAuthError" for entry in logs)
    signature = bybit.api_requests[0].headers["X-BAPI-SIGN"]
    dumped = json.dumps(logs, default=str) + str(info.value) + repr(client)
    assert API_KEY not in dumped
    assert API_SECRET not in dumped
    assert signature not in dumped
