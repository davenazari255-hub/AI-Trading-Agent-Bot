"""Operator authentication tests (WO-4).

Covers AC-OA-001.2 to AC-OA-001.4, AC-OA-002.2 to AC-OA-002.6, and AC-OA-003.1.
"""

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mooo_api.accounts import (
    AccountExistsError,
    InMemoryOperatorAccountStore,
    SqlOperatorAccountStore,
)
from mooo_api.app import PUBLIC_PATHS, create_app
from mooo_api.deps import require_session
from mooo_api.sessions import SESSION_COOKIE, SESSION_TTL_S, session_key
from mooo_core.bus import COMMANDS_STREAM, worker_heartbeat_key
from mooo_core.config import Settings, WorkerRole
from mooo_core.models import EventCategory

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
USERNAME = "operator"
PASSWORD = "correct horse battery staple"
WRONG = "wrong password!"
BASE_URL = "https://testserver"
LOGIN = "/api/v1/auth/login"
SETUP = "/api/v1/auth/setup"
ME = "/api/v1/auth/me"
LOCKOUT_S = 15 * 60


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(
        self,
        environment: Any,
        category: EventCategory,
        message: str,
        *,
        symbol: str | None = None,
        correlation_id: Any = None,
        refs: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "environment": environment,
                "category": category,
                "message": message,
                "refs": dict(refs or {}),
            }
        )

    def security(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["category"] == EventCategory.SECURITY]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def app(settings: Settings, redis: FakeRedis, clock: Clock, events: RecordingEvents) -> FastAPI:
    return create_app(
        settings,
        redis=redis,
        account_store=InMemoryOperatorAccountStore(),
        event_recorder=events,
        password_hasher=FAST_HASHER,
        clock=clock,
    )


def _client(app: FastAPI, ip: str = "10.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 50_000))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL)


def _creds(username: str = USERNAME, password: str = PASSWORD) -> dict[str, str]:
    return {"username": username, "password": password}


async def _setup(app: FastAPI) -> None:
    async with _client(app, ip="10.9.9.9") as client:
        response = await client.post(SETUP, json=_creds())
        assert response.status_code == 201


async def test_status_reports_setup_required_before_setup(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/api/v1/auth/status")
    assert response.status_code == 200
    assert response.json() == {"setup_required": True, "authenticated": False}


async def test_setup_creates_account_and_starts_session(
    app: FastAPI, events: RecordingEvents
) -> None:
    async with _client(app) as client:
        response = await client.post(SETUP, json=_creds())
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == USERNAME
        assert len(body["csrf_token"]) >= 43
        cookie_header = response.headers["set-cookie"].lower()
        expected = ("httponly", "secure", "samesite=strict", f"max-age={SESSION_TTL_S}")
        for attribute in expected:
            assert attribute in cookie_header
        assert len(client.cookies[SESSION_COOKIE]) >= 43
        me = await client.get(ME)
        assert me.status_code == 200
        assert me.json()["username"] == USERNAME
        status = await client.get("/api/v1/auth/status")
        assert status.json() == {"setup_required": False, "authenticated": True}
    messages = [event["message"] for event in events.security()]
    assert messages == ["Operator Account created by First-Run Setup"]
    assert PASSWORD not in repr(events.events)


async def test_setup_rejects_password_shorter_than_12_characters(app: FastAPI) -> None:
    async with _client(app) as client:
        short = await client.post(SETUP, json=_creds(password="elevenchars"))
        assert short.status_code == 422
        assert short.json()["error_code"] == "password_too_short"
        assert "at least 12 characters" in short.json()["message"]
        assert SESSION_COOKIE not in client.cookies
        status = await client.get("/api/v1/auth/status")
        assert status.json()["setup_required"] is True
        exact = await client.post(SETUP, json=_creds(password="twelve-chars"))
        assert exact.status_code == 201


async def test_setup_is_refused_once_an_account_exists(app: FastAPI) -> None:
    await _setup(app)
    async with _client(app, ip="10.0.0.2") as client:
        again = await client.post(SETUP, json=_creds("intruder", "another long password"))
        assert again.status_code == 409
        assert again.json()["error_code"] == "setup_already_completed"
        assert SESSION_COOKIE not in client.cookies
        takeover = await client.post(LOGIN, json=_creds("intruder", "another long password"))
        assert takeover.status_code == 401
        login = await client.post(LOGIN, json=_creds())
        assert login.status_code == 200


async def test_invalid_credentials_get_one_generic_message(app: FastAPI) -> None:
    await _setup(app)
    async with _client(app) as client:
        wrong_password = await client.post(LOGIN, json=_creds(password=WRONG))
        wrong_username = await client.post(LOGIN, json=_creds(username="nobody"))
    assert wrong_password.status_code == 401
    assert wrong_username.status_code == 401
    assert wrong_password.json() == wrong_username.json()
    assert wrong_password.json()["error_code"] == "invalid_credentials"
    assert "set-cookie" not in wrong_password.headers
    assert WRONG not in wrong_password.text


async def test_five_failures_from_one_ip_block_sign_in_for_15_minutes(
    app: FastAPI, events: RecordingEvents, clock: Clock
) -> None:
    await _setup(app)
    async with _client(app, ip="203.0.113.7") as attacker:
        statuses = []
        for attempt in range(5):
            response = await attacker.post(LOGIN, json=_creds(f"guess{attempt}", WRONG))
            statuses.append(response.status_code)
        assert statuses == [401, 401, 401, 401, 429]
        blocked = await attacker.post(LOGIN, json=_creds())
        assert blocked.status_code == 429
        assert blocked.json()["error_code"] == "sign_in_locked"
        assert 0 < int(blocked.headers["retry-after"]) <= LOCKOUT_S
        async with _client(app, ip="198.51.100.9") as operator:
            assert (await operator.post(LOGIN, json=_creds())).status_code == 200
        clock.now += LOCKOUT_S - 1
        assert (await attacker.post(LOGIN, json=_creds())).status_code == 429
        clock.now += 2
        assert (await attacker.post(LOGIN, json=_creds())).status_code == 200
    ip_events = [e for e in events.security() if e["refs"].get("scope") == "ip"]
    assert len(ip_events) == 1
    assert ip_events[0]["refs"]["client_ip"] == "203.0.113.7"
    assert WRONG not in repr(events.events)


async def test_five_failures_for_one_username_block_that_username(
    app: FastAPI, events: RecordingEvents
) -> None:
    await _setup(app)
    for attempt in range(5):
        async with _client(app, ip=f"192.0.2.{attempt + 1}") as client:
            await client.post(LOGIN, json=_creds(password=WRONG))
    async with _client(app, ip="192.0.2.200") as client:
        blocked = await client.post(LOGIN, json=_creds("  OPERATOR ", PASSWORD))
        assert blocked.status_code == 429
        other = await client.post(LOGIN, json=_creds("someone", PASSWORD))
        assert other.status_code == 401
    scopes = [event["refs"]["scope"] for event in events.security()]
    assert scopes.count("username") == 1


async def test_failures_spread_over_more_than_15_minutes_do_not_block(
    app: FastAPI, clock: Clock
) -> None:
    await _setup(app)
    async with _client(app) as client:
        for _ in range(4):
            assert (await client.post(LOGIN, json=_creds(password=WRONG))).status_code == 401
        clock.now += LOCKOUT_S + 1
        assert (await client.post(LOGIN, json=_creds(password=WRONG))).status_code == 401
        assert (await client.post(LOGIN, json=_creds())).status_code == 200


async def test_sign_out_ends_session_and_leaves_the_agent_untouched(
    app: FastAPI, redis: FakeRedis
) -> None:
    await _setup(app)
    heartbeat = worker_heartbeat_key(WorkerRole.TRADING)
    await redis.set(heartbeat, '{"ts": 0, "lock_held": true}')
    async with _client(app) as client:
        login = await client.post(LOGIN, json=_creds())
        csrf = login.json()["csrf_token"]
        session_id = client.cookies[SESSION_COOKIE]
        assert await redis.exists(session_key(session_id)) == 1
        no_csrf = await client.post("/api/v1/auth/logout")
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error_code"] == "csrf_failed"
        assert (await client.get(ME)).status_code == 200
        response = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 204
        assert await redis.exists(session_key(session_id)) == 0
    async with _client(app) as replay_client:
        replay = await replay_client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}={session_id}"})
        assert replay.status_code == 401
    assert await redis.exists(heartbeat) == 1
    assert await redis.exists(COMMANDS_STREAM) == 0


async def test_session_expires_12_hours_after_sign_in(
    app: FastAPI, redis: FakeRedis, clock: Clock
) -> None:
    await _setup(app)
    async with _client(app) as client:
        assert (await client.post(LOGIN, json=_creds())).status_code == 200
        session_id = client.cookies[SESSION_COOKIE]
        ttl = await redis.ttl(session_key(session_id))
        assert SESSION_TTL_S - 5 <= ttl <= SESSION_TTL_S
        clock.now += SESSION_TTL_S - 1
        assert (await client.get(ME)).status_code == 200
        clock.now += 2
        expired = await client.get(ME)
        assert expired.status_code == 401
        assert expired.json()["error_code"] == "unauthenticated"


async def test_requests_without_a_valid_session_are_denied(app: FastAPI) -> None:
    async with _client(app) as client:
        missing = await client.get(ME)
        forged = await client.get(ME, headers={"Cookie": f"{SESSION_COOKIE}=forged"})
        logout = await client.post("/api/v1/auth/logout")
    for response in (missing, forged, logout):
        assert response.status_code == 401
        assert response.json() == {
            "error_code": "unauthenticated",
            "message": "Sign in to continue.",
        }


def _dependency_calls(dependant: Any) -> list[Any]:
    calls: list[Any] = []
    for dependency in dependant.dependencies:
        calls.append(dependency.call)
        calls.extend(_dependency_calls(dependency))
    return calls


def test_every_route_except_public_ones_requires_a_session(app: FastAPI) -> None:
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    paths = {route.path for route in routes}
    assert {ME, "/api/v1/auth/logout"} <= paths
    assert PUBLIC_PATHS <= paths
    for route in routes:
        guarded = require_session in _dependency_calls(route.dependant)
        assert guarded is (route.path not in PUBLIC_PATHS), route.path


async def test_verify_password_checks_the_operator_password(app: FastAPI) -> None:
    service = app.state.auth_service
    assert await service.verify_password(PASSWORD) is False
    await _setup(app)
    assert await service.verify_password(PASSWORD) is True
    assert await service.verify_password(WRONG) is False


async def test_passwords_are_stored_as_argon2id_hashes() -> None:
    store = InMemoryOperatorAccountStore()
    app = create_app(
        Settings(_env_file=None, mooo_master_key="A" * 43 + "="),
        redis=FakeRedis(decode_responses=True),
        account_store=store,
        event_recorder=RecordingEvents(),
        password_hasher=FAST_HASHER,
    )
    await _setup(app)
    account = await store.get()
    assert account is not None
    assert account.password_hash.startswith("$argon2id$")
    assert PASSWORD not in account.password_hash


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ.get("MOOO_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("MOOO_TEST_DATABASE_URL must be set in CI")
        pytest.skip("set MOOO_TEST_DATABASE_URL to a migrated database to run this test")
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM operator_accounts"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM operator_accounts"))
        await engine.dispose()


async def test_sql_store_keeps_a_single_operator_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlOperatorAccountStore(session_factory)
    assert await store.get() is None
    created = await store.create(USERNAME, FAST_HASHER.hash(PASSWORD))
    assert await store.get() == created
    with pytest.raises(AccountExistsError):
        await store.create("second", FAST_HASHER.hash(PASSWORD))
    loaded = await store.get()
    assert loaded is not None
    assert loaded.username == USERNAME
