"""Connector toolkit (Chapter 6): auth strategies, retries, breaker, HTTP base.

THE PROBLEM: every outside system is slow, flaky, differently authenticated and
speaks its own schema. If OrderService called httpx directly it would be
welded to one vendor and to every failure mode. A CONNECTOR is an *adapter*:

    OrderService --(port: PaymentPort, our vocabulary)--> PaymentConnector --HTTP--> vendor

The port is a typing.Protocol owned by us; the connector translates our calls
and the vendor's JSON in both directions, and hides auth + resilience.

EXECUTION FLOW of BaseHttpConnector.request():
    breaker.allow? --no--> CircuitOpenError (fail fast, don't hammer a dead host)
          | yes
    auth.headers() (fetches/refreshes token if needed)
          |
    httpx send with timeout
          |-- 401 -> invalidate token, retry ONCE with a fresh one
          |-- timeout / connect error / 429 / 502 / 503 / 504 -> record failure,
          |      sleep(exponential backoff + jitter), try again (if safe)
          |-- other 4xx -> caller's fault: raise ConnectorHTTPError, no retry
          `-- 2xx -> record success, return
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from app.core.observability import CONNECTOR_CALLS, log_event

log = logging.getLogger("connector")

# ------------------------------------------------------------------ errors


class ConnectorError(Exception):
    """Base for everything a connector can raise."""


class ConnectorTimeout(ConnectorError):
    pass


class ConnectorUnavailable(ConnectorError):
    """Retries exhausted on transient failures."""


class ConnectorAuthError(ConnectorError):
    pass


class CircuitOpenError(ConnectorError):
    pass


class ConnectorHTTPError(ConnectorError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


# ------------------------------------------------------------------ retry


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with FULL jitter.

    delay(n) = random(0, min(max_delay, base * 2**(n-1))).
    Backoff gives the vendor time to recover; jitter stops 1000 clients that
    failed together from retrying together (thundering herd).
    """

    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: bool = True

    def delay(self, attempt: int) -> float:
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling) if self.jitter else ceiling


RETRYABLE_STATUS = {429, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}

# ------------------------------------------------------------------ circuit breaker


class BreakerState(StrEnum):
    CLOSED = "closed"  # normal
    OPEN = "open"  # failing: reject immediately
    HALF_OPEN = "half_open"  # probing: let ONE call through


class CircuitBreaker:
    """Stop calling a dependency that is clearly down.

    CLOSED --(N consecutive failures)--> OPEN --(reset_seconds)--> HALF_OPEN
    HALF_OPEN --success--> CLOSED ; HALF_OPEN --failure--> OPEN
    WHY: without it, every request waits for the full timeout x retries while
    the dependency is down, exhausting workers and turning one outage into two.
    """

    def __init__(self, failure_threshold: int = 5, reset_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self.state = BreakerState.CLOSED

    def allow(self) -> bool:
        if self.state is BreakerState.OPEN:
            if self._clock() - self._opened_at >= self.reset_seconds:
                self.state = BreakerState.HALF_OPEN  # let a probe through
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self.state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self.state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self._opened_at = self._clock()


# ------------------------------------------------------------------ auth strategies


class AuthStrategy(Protocol):
    async def headers(self) -> dict[str, str]: ...
    async def invalidate(self) -> bool:
        """Drop cached credentials. Return True if a retry could help."""
        ...


class NoAuth:
    async def headers(self) -> dict[str, str]:
        return {}

    async def invalidate(self) -> bool:
        return False


class ApiKeyAuth:
    """Static key in a header. Simple; rotate by redeploying/secret update."""

    def __init__(self, key: str, header: str = "X-API-Key"):
        self._key, self._header = key, header

    async def headers(self) -> dict[str, str]:
        return {self._header: self._key}

    async def invalidate(self) -> bool:
        return False


class BearerAuth:
    """Pre-issued static bearer token (e.g. a personal access token)."""

    def __init__(self, token: str):
        self._token = token

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def invalidate(self) -> bool:
        return False


class OAuth2ClientCredentials:
    """OAuth2 client-credentials grant (machine-to-machine) with caching.

    FLOW: POST token_url (client_id+secret) -> {access_token, expires_in}.
    We cache the token and refresh *before* expiry (`skew` seconds early) so a
    request never leaves with a token that dies in flight. A lock makes
    concurrent callers share ONE refresh instead of stampeding the auth server.
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str, *,
                 scope: str = "", skew: float = 30.0, timeout: float = 5.0,
                 clock: Callable[[], float] = time.monotonic,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._url, self._id, self._secret, self._scope = token_url, client_id, client_secret, scope
        self._skew, self._timeout, self._clock, self._transport = skew, timeout, clock, transport
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.fetch_count = 0  # observable in tests

    async def headers(self) -> dict[str, str]:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - self._skew:
                await self._fetch()
            return {"Authorization": f"Bearer {self._token}"}

    async def invalidate(self) -> bool:
        async with self._lock:
            self._token = None  # server said 401: our cached token is not trusted
        return True

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
            try:
                r = await c.post(self._url, data={
                    "grant_type": "client_credentials", "client_id": self._id,
                    "client_secret": self._secret, "scope": self._scope})
            except httpx.HTTPError as exc:
                raise ConnectorAuthError(f"token endpoint unreachable: {exc}") from exc
        if r.status_code != 200:
            raise ConnectorAuthError(f"token endpoint returned {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 300))
        self.fetch_count += 1


# ------------------------------------------------------------------ base connector


class BaseHttpConnector:
    name = "http"

    def __init__(self, base_url: str, *, auth: AuthStrategy | None = None,
                 retry: RetryPolicy | None = None, breaker: CircuitBreaker | None = None,
                 timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.auth = auth or NoAuth()
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker()
        self._sleep = sleep  # injectable => tests run instantly
        # ONE long-lived AsyncClient = connection pooling + keep-alive. Creating a
        # client per request is a classic performance bug.
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport,
            # granular timeouts: fail fast on connect, allow time to read
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, json=None, params=None,
                      headers: dict[str, str] | None = None, data=None,
                      idempotent: bool | None = None) -> httpx.Response:
        # Only retry when repeating the call cannot do harm: naturally idempotent
        # methods, or POSTs the caller marks idempotent (they carry an Idempotency-Key).
        safe = idempotent if idempotent is not None else method.upper() in IDEMPOTENT_METHODS
        attempts = self.retry.max_attempts if safe else 1
        auth_retried = False
        last_exc: Exception | None = None

        attempt = 0
        while attempt < attempts:
            attempt += 1
            if not self.breaker.allow():
                CONNECTOR_CALLS.labels(self.name, "circuit_open").inc()
                raise CircuitOpenError(f"{self.name}: circuit open, failing fast")
            try:
                hdrs = {**(await self.auth.headers()), **(headers or {})}
                resp = await self._client.request(method, path, json=json, params=params,
                                                  headers=hdrs, data=data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.breaker.record_failure()
                last_exc = ConnectorTimeout(f"{self.name}: {type(exc).__name__}")
                last_exc.__cause__ = exc
            else:
                if resp.status_code == 401 and not auth_retried and await self.auth.invalidate():
                    auth_retried = True  # expired/revoked token: refresh once, free of charge
                    attempt -= 1
                    continue
                if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
                    self.breaker.record_failure()
                    last_exc = ConnectorUnavailable(f"{self.name}: HTTP {resp.status_code}")
                    if resp.status_code not in RETRYABLE_STATUS:
                        break  # a plain 500 is not safe to blindly repeat
                elif resp.status_code >= 400:
                    self.breaker.record_success()  # the service is up; *we* sent a bad request
                    CONNECTOR_CALLS.labels(self.name, f"http_{resp.status_code}").inc()
                    if resp.status_code == 401:
                        raise ConnectorAuthError(f"{self.name}: unauthorized")
                    raise ConnectorHTTPError(resp.status_code, resp.text)
                else:
                    self.breaker.record_success()
                    CONNECTOR_CALLS.labels(self.name, "ok").inc()
                    return resp
            if attempt < attempts:
                delay = self.retry.delay(attempt)
                log_event(log, "connector_retry", connector=self.name, attempt=attempt, delay=round(delay, 3))
                await self._sleep(delay)

        CONNECTOR_CALLS.labels(self.name, "failed").inc()
        assert last_exc is not None
        raise last_exc
