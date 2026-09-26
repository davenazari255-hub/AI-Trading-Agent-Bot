"""Operator authentication routes under ``/api/v1/auth``.

``public_router`` holds the routes that work without a session (setup, login,
status). ``protected_router`` must be included with the SessionDependency.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from mooo_api.deps import CurrentSession, get_auth_service, get_session_store
from mooo_api.sessions import SESSION_COOKIE, OperatorSession

AUTH_PREFIX = "/api/v1/auth"
PUBLIC_AUTH_PATHS = (f"{AUTH_PREFIX}/setup", f"{AUTH_PREFIX}/login", f"{AUTH_PREFIX}/status")

public_router = APIRouter(prefix=AUTH_PREFIX, tags=["auth"])
protected_router = APIRouter(prefix=AUTH_PREFIX, tags=["auth"])


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=256)
    password: SecretStr


class SessionInfo(BaseModel):
    username: str
    csrf_token: str
    expires_at: datetime


class AuthStatus(BaseModel):
    setup_required: bool
    authenticated: bool


def _session_info(session: OperatorSession) -> SessionInfo:
    return SessionInfo(
        username=session.username,
        csrf_token=session.csrf_token,
        expires_at=datetime.fromtimestamp(session.expires_at, UTC),
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _set_session_cookie(response: Response, session: OperatorSession, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session.session_id,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


@public_router.post("/setup", status_code=201)
async def setup(body: Credentials, request: Request, response: Response) -> SessionInfo:
    service = get_auth_service(request)
    session = await service.setup(body.username, body.password.get_secret_value())
    _set_session_cookie(response, session, service.session_ttl_s)
    return _session_info(session)


@public_router.post("/login")
async def login(body: Credentials, request: Request, response: Response) -> SessionInfo:
    service = get_auth_service(request)
    session = await service.login(
        body.username, body.password.get_secret_value(), _client_ip(request)
    )
    _set_session_cookie(response, session, service.session_ttl_s)
    return _session_info(session)


@public_router.get("/status")
async def auth_status(request: Request) -> AuthStatus:
    session = await get_session_store(request).get(request.cookies.get(SESSION_COOKIE))
    setup_required = await get_auth_service(request).setup_required()
    return AuthStatus(setup_required=setup_required, authenticated=session is not None)


@protected_router.get("/me")
async def me(session: CurrentSession) -> SessionInfo:
    return _session_info(session)


@protected_router.post("/logout", status_code=204)
async def logout(session: CurrentSession, request: Request) -> Response:
    await get_auth_service(request).logout(session.session_id)
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
    )
    return response
