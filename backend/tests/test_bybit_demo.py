"""Opt-in integration tests against Bybit Demo Trading (WO-5).

Run with ``pytest -m demo``. The signed test needs DEMO keys in the environment:
BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET. Never use Live keys here.
"""

import os

import pytest

from mooo_core.exchange.errors import ForbiddenEndpointError
from mooo_core.exchange.rest import DEMO_BASE_URL, BybitRestClient

pytestmark = pytest.mark.demo


def _demo_credentials() -> tuple[str, str]:
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET to run Bybit Demo tests")
    return api_key, api_secret


async def test_demo_server_time_sync() -> None:
    async with BybitRestClient(base_url=DEMO_BASE_URL) as client:
        offset = await client.sync_time()
    assert abs(offset) < 60_000


async def test_demo_public_tickers() -> None:
    async with BybitRestClient(base_url=DEMO_BASE_URL) as client:
        result = await client.get("/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"})
    assert result["list"][0]["symbol"] == "BTCUSDT"


async def test_demo_signed_wallet_balance() -> None:
    api_key, api_secret = _demo_credentials()
    async with BybitRestClient(
        base_url=DEMO_BASE_URL, api_key=api_key, api_secret=api_secret
    ) as client:
        await client.sync_time()
        result = await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        with pytest.raises(ForbiddenEndpointError):
            await client.post("/v5/asset/transfer/inter-transfer", {})
    assert "list" in result
