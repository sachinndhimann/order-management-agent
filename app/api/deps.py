"""FastAPI dependency providers (Chapter 2).

DEPENDENCY INJECTION in one paragraph: a path function declares what it NEEDS
(`order_service: OrderService = Depends(get_order_service)`); FastAPI inspects
the signature, builds a graph, resolves leaves first, caches each result for the
request (use_cache=True) and calls cleanup of `yield` dependencies in reverse
order. The endpoint never constructs or looks up anything itself, which is what
makes `app.dependency_overrides[...] = fake` possible in tests.

GRAPH for POST /orders:
    get_current_principal  <- oauth2_scheme, get_settings
    require_roles(...)     <- get_current_principal           (class-based, parameterised)
    get_order_service      <- get_session <- get_container ;  get_container
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.config import Settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import Principal, Role, decode_access_token
from app.db.session import session_scope
from app.schemas.common import PageParams
from app.services.factory import build_inventory_service, build_order_service
from app.services.order_service import InventoryService, OrderService

# tokenUrl powers the "Authorize" button in /docs (OAuth2 password flow).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container  # set in lifespan => no global needed in the API


def get_settings(c: Annotated[Container, Depends(get_container)]) -> Settings:
    return c.settings


async def get_session(c: Annotated[Container, Depends(get_container)]) -> AsyncIterator[AsyncSession]:
    """YIELD dependency = setup / teardown. Code after `yield` is the cleanup:
    commit on success, rollback on error, always return the connection to the pool."""
    async with session_scope(c.sessionmaker) as session:
        yield session


# scope="function": run the teardown (the COMMIT!) BEFORE the response is sent.
# Default scope would commit after the client already got a 201 - a failed
# commit would then be invisible to the caller.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_order_service(session: SessionDep, c: Annotated[Container, Depends(get_container)]) -> OrderService:
    return build_order_service(session, c)


def get_inventory_service(session: SessionDep) -> InventoryService:
    return build_inventory_service(session)


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]


async def get_current_principal(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """AUTHENTICATION: who are you? Stateless - verifies the JWT signature and
    expiry, no DB lookup. Trade-off: role changes apply when the token expires."""
    if not token:
        raise AuthenticationError("Missing bearer token")
    return decode_access_token(token, settings)


CurrentUser = Annotated[Principal, Depends(get_current_principal)]


class RoleChecker:
    """CLASS-BASED dependency: __init__ takes configuration (allowed roles),
    __call__ is what FastAPI invokes per request. One class, many guards:
        Depends(RoleChecker(Role.ADMIN, Role.STAFF))
    AUTHORIZATION: what may you do?"""

    def __init__(self, *allowed: Role):
        self.allowed = set(allowed)

    def __call__(self, user: CurrentUser) -> Principal:
        if user.role not in self.allowed:
            raise PermissionDeniedError(f"Role '{user.role.value}' may not perform this action")
        return user


require_staff = RoleChecker(Role.ADMIN, Role.STAFF)
require_admin = RoleChecker(Role.ADMIN)


def pagination(limit: Annotated[int, Query(ge=1, le=100)] = 20,
               offset: Annotated[int, Query(ge=0)] = 0) -> PageParams:
    """Reusable query-param bundle (function dependency)."""
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(pagination)]
