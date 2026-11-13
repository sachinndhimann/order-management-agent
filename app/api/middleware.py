"""ASGI middleware (Chapter 1): cross-cutting concerns around EVERY request.

Pure ASGI (not BaseHTTPMiddleware) so it doesn't break background tasks or
contextvars and adds no extra task per request.

ORDER MATTERS: add_middleware() wraps outward, so the LAST added runs FIRST.
We add CORS last so even error responses get CORS headers.
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.observability import HTTP_LATENCY, HTTP_REQUESTS, log_event, request_id_var

log = logging.getLogger("http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Honour an upstream id (gateway / caller) so traces stitch across services.
        rid = Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            # route TEMPLATE ("/orders/{order_id}") not raw path: keeps metric cardinality bounded.
            route = getattr(scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(scope["method"], route, str(status)).inc()
            HTTP_LATENCY.labels(route).observe(elapsed)
            log_event(log, "request", method=scope["method"], path=scope["path"], status=status,
                      ms=round(elapsed * 1000, 1))
            request_id_var.reset(token)
