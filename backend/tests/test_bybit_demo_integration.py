"""Opt-in integration tests for BybitRestClient against Bybit Demo Trading (WO-5).

Run with ``pytest -m demo``. They need a Bybit Demo Trading key in BYBIT_DEMO_API_KEY and
BYBIT_DEMO_API_SECRET. A read-only key is enough: these tests never place orders.
"""

import os
import time
from collections.abc import AsyncIterator

import pytest

from mooo_core.exchange import (
    BybitNetwork,
    BybitRestClient,
    ExchangeAuthError,
    ForbiddenEndpointError,
)

DEMO_KEY = os.environ.get("BYBIT_DEMO_API_KEY", "")
DEMO_SECRET = os.environ.get("BYBIT_DEMO_API_SECRET", "")

pytestmark = [
    pytest.mark.demo,
    pytest.mark.skipif(
        not (DEMO_KEY and DEMO_SECRET),
        reason="set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET to run Bybit Demo tests",
    ),
]


@pytest.fixture
async def demo_client() -> AsyncIterator[BybitRestClient]:
    client = BybitRestClient(BybitNetwork.DEMO, api_key=DEMO_KEY, api_secret=DEMO_SECRET)
    try:
        yield client
    finally:
        await client.aclose()


async def test_server_time_sync(demo_client: BybitRestClient) -> None:
    server_ms = await demo_client.sync_time()
    assert abs(server_ms - time.time() * 1000) < 60_000


async def test_public_instruments(demo_client: BybitRestClient) -> None:
    response = await demo_client.get(
        "/v5/market/instruments-info", {"category": "linear", "symbol": "BTCUSDT"}
    )
    assert response.result["list"][0]["symbol"] == "BTCUSDT"


async def test_signed_wallet_balance(demo_client: BybitRestClient) -> None:
    response = await demo_client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    assert "list" in response.result


async def test_signed_api_key_info(demo_client: BybitRestClient) -> None:
    response = await demo_client.get("/v5/user/query-api")
    assert "permissions" in response.result


async def test_signed_open_orders(demo_client: BybitRestClient) -> None:
    response = await demo_client.get(
        "/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"}
    )
    assert "list" in response.result


async def test_wrong_secret_maps_to_auth_error() -> None:
    client = BybitRestClient(
        BybitNetwork.DEMO, api_key=DEMO_KEY, api_secret="not-the-real-demo-secret"
    )
    try:
        with pytest.raises(ExchangeAuthError):
            await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    finally:
        await client.aclose()


async def test_withdrawal_is_unreachable(demo_client: BybitRestClient) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await demo_client.post("/v5/asset/withdraw/create", {"coin": "USDT"})
