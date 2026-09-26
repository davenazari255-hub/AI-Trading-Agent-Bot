"""SessionDependency: protected routes need a valid Operator Session.

State-changing requests (POST, PUT, PATCH, DELETE) also need the session's CSRF
token in the ``X-CSRF-Token`` header.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Request

from mooo_api.auth_service import AuthService
from mooo_api.errors import ApiError
from mooo_api.sessions import CSRF_HEADER, SESSION_COOKIE, OperatorSession, SessionStore

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
    return service


async def require_session(request: Request) -> OperatorSession:
    session = await get_session_store(request).get(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    if request.method in UNSAFE_METHODS and not _csrf_matches(request, session):
        raise ApiError(403, "csrf_failed", "The request needs a valid CSRF token.")
    return session


def _csrf_matches(request: Request, session: OperatorSession) -> bool:
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        return False
    return secrets.compare_digest(supplied.encode(), session.csrf_token.encode())


CurrentSession = Annotated[OperatorSession, Depends(require_session)]
