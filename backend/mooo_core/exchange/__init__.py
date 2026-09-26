"""Bybit exchange access (Exchange Adapter blueprint).

Every Bybit REST call goes through :class:`BybitRestClient`. The adapter interface, the
WebSocket streams, and credential storage live in other modules.
"""

from mooo_core.exchange.endpoints import (
    DEFAULT_GROUP_LIMITS,
    GLOBAL_LIMIT,
    PRIVATE_ENDPOINTS,
    EndpointGroup,
    EndpointSpec,
    GroupLimit,
    is_allowed_endpoint,
    resolve_endpoint,
)
from mooo_core.exchange.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimitError,
    ExchangeRejectionError,
    ExchangeTemporaryError,
    ForbiddenEndpointError,
    error_for_ret_code,
)
from mooo_core.exchange.rate_limit import RateLimiter, TokenBucket
from mooo_core.exchange.rest_client import (
    BASE_URLS,
    RECV_WINDOW_MS,
    BybitNetwork,
    BybitResponse,
    BybitRestClient,
    RateLimitStatus,
    encode_body,
    encode_query,
    sign_payload,
)

__all__ = [
    "BASE_URLS",
    "DEFAULT_GROUP_LIMITS",
    "GLOBAL_LIMIT",
    "PRIVATE_ENDPOINTS",
    "RECV_WINDOW_MS",
    "BybitNetwork",
    "BybitResponse",
    "BybitRestClient",
    "EndpointGroup",
    "EndpointSpec",
    "ExchangeAuthError",
    "ExchangeError",
    "ExchangeRateLimitError",
    "ExchangeRejectionError",
    "ExchangeTemporaryError",
    "ForbiddenEndpointError",
    "GroupLimit",
    "RateLimitStatus",
    "RateLimiter",
    "TokenBucket",
    "encode_body",
    "encode_query",
    "error_for_ret_code",
    "is_allowed_endpoint",
    "resolve_endpoint",
    "sign_payload",
]
