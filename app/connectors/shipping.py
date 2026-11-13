"""Generic REST API connector (Chapter 6): shipping-rate lookup.

Shows the smallest useful REST connector: API-key auth, GET with query params,
and a transformation of vendor strings ("12.50") into integer cents.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.connectors.base import (
    ApiKeyAuth, BaseHttpConnector, CircuitBreaker, ConnectorError, RetryPolicy,
)
from app.core.config import Settings
from app.core.errors import UpstreamUnavailableError


@dataclass(frozen=True)
class ShippingQuote:
    carrier: str
    cost_cents: int
    eta_days: int


class ShippingPort(Protocol):
    async def quote(self, *, postal_code: str, weight_kg: float) -> ShippingQuote: ...
    async def aclose(self) -> None: ...


class ShippingConnector(BaseHttpConnector):
    name = "shipping"

    @classmethod
    def from_settings(cls, s: Settings, **overrides) -> ShippingConnector:
        return cls(s.shipping_base_url, auth=ApiKeyAuth(s.shipping_api_key.get_secret_value()),
                   timeout=s.http_timeout_seconds,
                   retry=RetryPolicy(s.retry_max_attempts, s.retry_base_delay),
                   breaker=CircuitBreaker(s.breaker_failure_threshold, s.breaker_reset_seconds),
                   **overrides)

    async def quote(self, *, postal_code: str, weight_kg: float) -> ShippingQuote:
        try:
            resp = await self.request("GET", "/rates", params={"zip": postal_code, "kg": weight_kg})
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"Shipping API unavailable: {exc}") from exc
        rates = resp.json()["rates"]
        cheapest = min(rates, key=lambda r: Decimal(r["price"]))
        # Decimal, never float, for money: Decimal("12.50") * 100 == 1250 exactly.
        return ShippingQuote(cheapest["carrier"], int(Decimal(cheapest["price"]) * 100), int(cheapest["eta_days"]))
