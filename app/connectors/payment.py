"""External payment API connector (Chapter 6).

Adapter pattern in practice:
  PaymentPort  - what OUR domain needs (charge, refund). Owned by us.
  PaymentConnector - speaks the vendor's HTTP/JSON dialect (OAuth2, cents,
                     "succeeded"/"declined" strings) and translates.
Swapping vendors == writing another class that satisfies PaymentPort.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.connectors.base import (
    BaseHttpConnector, CircuitBreaker, ConnectorError, ConnectorHTTPError,
    OAuth2ClientCredentials, RetryPolicy,
)
from app.core.config import Settings
from app.core.errors import PaymentFailedError, UpstreamUnavailableError


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class PaymentResult:  # internal DTO - vendor field names never leak past this file
    reference: str
    status: PaymentStatus
    amount_cents: int


class PaymentPort(Protocol):
    async def charge(self, *, order_id: str, amount_cents: int, currency: str, customer_email: str) -> PaymentResult: ...
    async def refund(self, *, payment_ref: str, amount_cents: int) -> str: ...
    async def aclose(self) -> None: ...


class PaymentConnector(BaseHttpConnector):
    name = "payment"

    @classmethod
    def from_settings(cls, s: Settings, *, transport=None, **overrides) -> PaymentConnector:
        auth = OAuth2ClientCredentials(
            f"{s.payment_base_url}/oauth/token", s.payment_client_id,
            s.payment_client_secret.get_secret_value(), scope="charges refunds",
            timeout=s.http_timeout_seconds, transport=transport)  # transport hook => tests can mount a fake ASGI vendor
        return cls(s.payment_base_url, auth=auth, timeout=s.http_timeout_seconds, transport=transport,
                   retry=RetryPolicy(s.retry_max_attempts, s.retry_base_delay),
                   breaker=CircuitBreaker(s.breaker_failure_threshold, s.breaker_reset_seconds),
                   **overrides)

    async def charge(self, *, order_id: str, amount_cents: int, currency: str, customer_email: str) -> PaymentResult:
        try:
            # Idempotency-Key = order id: if we time out and retry, the vendor
            # recognises the repeat and does NOT charge twice. This is what makes
            # retrying a POST safe (hence idempotent=True).
            resp = await self.request(
                "POST", "/v1/charges", idempotent=True,
                headers={"Idempotency-Key": f"charge-{order_id}"},
                json={"amount": amount_cents, "currency": currency.lower(),
                      "receipt_email": customer_email, "metadata": {"order_id": order_id}})
        except ConnectorHTTPError as exc:
            if exc.status == 402:  # vendor says: declined -> business outcome, not an outage
                raise PaymentFailedError("Payment was declined", provider_status=exc.status) from exc
            raise UpstreamUnavailableError(f"Payment provider rejected request ({exc.status})") from exc
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"Payment provider unavailable: {exc}") from exc
        body = resp.json()  # schema transformation: vendor -> internal DTO
        return PaymentResult(
            reference=body["id"],
            status=PaymentStatus.SUCCEEDED if body["status"] == "succeeded" else PaymentStatus.FAILED,
            amount_cents=int(body["amount"]),
        )

    async def refund(self, *, payment_ref: str, amount_cents: int) -> str:
        try:
            resp = await self.request(
                "POST", "/v1/refunds", idempotent=True,
                headers={"Idempotency-Key": f"refund-{payment_ref}"},
                json={"charge": payment_ref, "amount": amount_cents})
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"Refund failed: {exc}") from exc
        return resp.json()["id"]
