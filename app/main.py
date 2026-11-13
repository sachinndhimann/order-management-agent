"""FastAPI application factory (Chapter 1: lifecycle, ASGI, middleware, OpenAPI).

ASGI in 5 lines: ASGI is the contract between a server (Uvicorn) and an async
Python app: `await app(scope, receive, send)`. Uvicorn parses HTTP and calls the
app once per request; FastAPI = Starlette (routing/middleware) + Pydantic
(validation) + OpenAPI generation, all behind that one callable. Async means one
process interleaves thousands of requests while each awaits I/O - but a blocking
call (time.sleep, requests.get, CPU loops) freezes ALL of them.

LIFESPAN: startup code before `yield`, shutdown after. Used to create the DB
engine + HTTP client pools once and close them cleanly (no leaked connections).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api.deps import get_container
from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.v1 import agent_router
from app.api.v1.routers import auth_router, orders_router, products_router, reports_router
from app.api.v2 import routers as v2
from app.container import Container, build_container
from app.core.config import get_settings
from app.core.observability import configure_logging

DESCRIPTION = """
AI Order Management Agent. REST API over the same services the **MCP server** exposes to LLMs.

Authenticate: `POST /api/v1/auth/token` (email as `username`), then click **Authorize**.
"""


def create_app(container: Container | None = None) -> FastAPI:
    settings = container.settings if container else get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container(settings)
        yield
        await app.state.container.aclose()

    app = FastAPI(title=settings.app_name, version="1.0.0", description=DESCRIPTION, lifespan=lifespan,
                  openapi_tags=[{"name": "orders", "description": "Create and manage orders"},
                                {"name": "inventory", "description": "Products and stock"},
                                {"name": "agent", "description": "Natural-language interface (LLM + MCP)"}])
    # Tests using httpx's ASGITransport do not run lifespan; make state available anyway.
    if container is not None:
        app.state.container = container

    register_exception_handlers(app)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["X-Request-ID", "Location", "Idempotent-Replay"])

    v1 = APIRouter(prefix="/api/v1")
    for r in (auth_router, products_router, orders_router, reports_router, agent_router.router):
        v1.include_router(r)
    app.include_router(v1)
    app.include_router(v2.router, prefix="/api/v2")

    @app.get("/health", tags=["ops"], summary="Liveness: is the process up?")
    async def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], summary="Readiness: can we serve traffic (DB reachable)?")
    async def ready(c: Container = Depends(get_container)):
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(_: Request):
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def get_app() -> FastAPI:
    """Entry point for `uvicorn app.main:get_app --factory`. A factory (not a
    module-level `app`) means importing this module has no side effects."""
    return create_app()
