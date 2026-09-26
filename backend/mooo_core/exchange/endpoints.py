"""Bybit V5 endpoint allow-list and rate-limit groups (Exchange Adapter blueprint).

Only the paths below can be requested. Withdrawal, transfer, sub-account, and API key
management endpoints are not listed, so the REST client cannot reach them by construction.

Rate limits are per endpoint group. Every group limit is at or below the lowest documented
Bybit limit of the endpoints in the group (https://bybit-exchange.github.io/docs/v5/rate-limit),
and the global limit stays below the HTTP IP limit of 600 requests per 5 seconds.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from mooo_core.exchange.errors import ForbiddenEndpointError

HttpMethod = Literal["GET", "POST"]


class EndpointGroup(StrEnum):
    MARKET = "market"
    ACCOUNT = "account"
    POSITION_QUERY = "position_query"
    POSITION_WRITE = "position_write"
    ORDER_WRITE = "order_write"
    ORDER_CANCEL_ALL = "order_cancel_all"
    ORDER_QUERY = "order_query"
    EXECUTION_QUERY = "execution_query"
    USER = "user"


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    path: str
    method: HttpMethod
    group: EndpointGroup
    signed: bool


@dataclass(frozen=True, slots=True)
class GroupLimit:
    """Token bucket settings: steady ``rate_per_s`` and a maximum ``burst``."""

    rate_per_s: float
    burst: float


MARKET_PREFIX = "/v5/market/"
# Public market data: one or two lowercase segments, for example /v5/market/kline or
# /v5/market/funding/history. No dots, query strings, encodings, or upper case.
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_MARKET_PATH = re.compile(rf"/v5/market/{_SEGMENT}(?:/{_SEGMENT})?")


def _signed(path: str, method: HttpMethod, group: EndpointGroup) -> tuple[str, EndpointSpec]:
    return path, EndpointSpec(path=path, method=method, group=group, signed=True)


PRIVATE_ENDPOINTS: Mapping[str, EndpointSpec] = MappingProxyType(
    dict(
        [
            _signed("/v5/account/wallet-balance", "GET", EndpointGroup.ACCOUNT),
            _signed("/v5/account/info", "GET", EndpointGroup.ACCOUNT),
            _signed("/v5/account/fee-rate", "GET", EndpointGroup.ACCOUNT),
            _signed("/v5/position/list", "GET", EndpointGroup.POSITION_QUERY),
            _signed("/v5/position/closed-pnl", "GET", EndpointGroup.POSITION_QUERY),
            _signed("/v5/position/set-leverage", "POST", EndpointGroup.POSITION_WRITE),
            _signed("/v5/position/trading-stop", "POST", EndpointGroup.POSITION_WRITE),
            _signed("/v5/position/switch-mode", "POST", EndpointGroup.POSITION_WRITE),
            _signed("/v5/order/create", "POST", EndpointGroup.ORDER_WRITE),
            _signed("/v5/order/amend", "POST", EndpointGroup.ORDER_WRITE),
            _signed("/v5/order/cancel", "POST", EndpointGroup.ORDER_WRITE),
            _signed("/v5/order/cancel-all", "POST", EndpointGroup.ORDER_CANCEL_ALL),
            _signed("/v5/order/realtime", "GET", EndpointGroup.ORDER_QUERY),
            _signed("/v5/order/history", "GET", EndpointGroup.ORDER_QUERY),
            _signed("/v5/execution/list", "GET", EndpointGroup.EXECUTION_QUERY),
            _signed("/v5/user/query-api", "GET", EndpointGroup.USER),
        ]
    )
)

DEFAULT_GROUP_LIMITS: Mapping[EndpointGroup, GroupLimit] = MappingProxyType(
    {
        # Public endpoints share the IP limit only; stay well below it.
        EndpointGroup.MARKET: GroupLimit(rate_per_s=20.0, burst=20.0),
        # wallet-balance and account/info: 50/s; fee-rate: 10/s.
        EndpointGroup.ACCOUNT: GroupLimit(rate_per_s=8.0, burst=8.0),
        # position/list and closed-pnl: 50/s.
        EndpointGroup.POSITION_QUERY: GroupLimit(rate_per_s=20.0, burst=20.0),
        # set-leverage, trading-stop, switch-mode: 10/s.
        EndpointGroup.POSITION_WRITE: GroupLimit(rate_per_s=8.0, burst=8.0),
        # order create, amend, cancel (linear): 10/s.
        EndpointGroup.ORDER_WRITE: GroupLimit(rate_per_s=8.0, burst=8.0),
        # cancel-all: conservative single request per second.
        EndpointGroup.ORDER_CANCEL_ALL: GroupLimit(rate_per_s=1.0, burst=1.0),
        # order realtime and history: 50/s.
        EndpointGroup.ORDER_QUERY: GroupLimit(rate_per_s=20.0, burst=20.0),
        # execution/list: 50/s.
        EndpointGroup.EXECUTION_QUERY: GroupLimit(rate_per_s=20.0, burst=20.0),
        # user/query-api: 10/s.
        EndpointGroup.USER: GroupLimit(rate_per_s=5.0, burst=5.0),
    }
)

# HTTP IP limit is 600 requests per 5 seconds (120/s). Applied to every request.
GLOBAL_LIMIT = GroupLimit(rate_per_s=100.0, burst=100.0)


def resolve_endpoint(method: str, path: str) -> EndpointSpec:
    """Return the allow-listed spec for ``method`` and ``path``.

    Raises ``ForbiddenEndpointError`` for any path or method that is not allowed.
    """
    spec = PRIVATE_ENDPOINTS.get(path)
    if spec is None and _MARKET_PATH.fullmatch(path):
        spec = EndpointSpec(path=path, method="GET", group=EndpointGroup.MARKET, signed=False)
    if spec is None:
        raise ForbiddenEndpointError(
            f"Endpoint is not on the Bybit allow-list: {path[:120]!r}", path=path[:120]
        )
    normalized_method = method.upper()
    if normalized_method != spec.method:
        raise ForbiddenEndpointError(
            f"Method {normalized_method[:10]!r} is not allowed for this endpoint", path=spec.path
        )
    return spec


def is_allowed_endpoint(method: str, path: str) -> bool:
    try:
        resolve_endpoint(method, path)
    except ForbiddenEndpointError:
        return False
    return True
