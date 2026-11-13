"""Simulated CRM connector (Chapter 6): API-key auth + heavy schema transformation.

The CRM names things PascalCase ("EmailAddress", "FirstName"); we use
snake_case domain DTOs. The translation lives ONLY here, so the rest of the app
never learns the CRM's vocabulary and a CRM change touches one file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.connectors.base import (
    ApiKeyAuth, BaseHttpConnector, CircuitBreaker, ConnectorError, ConnectorHTTPError, RetryPolicy,
)
from app.core.config import Settings
from app.core.errors import UpstreamUnavailableError


@dataclass(frozen=True)
class CustomerProfile:
    crm_id: str
    email: str
    full_name: str
    tier: str


class CRMPort(Protocol):
    async def upsert_customer(self, *, email: str, full_name: str | None = None) -> CustomerProfile: ...
    async def get_customer(self, email: str) -> CustomerProfile | None: ...
    async def aclose(self) -> None: ...


def to_profile(raw: dict) -> CustomerProfile:
    """Vendor JSON -> domain DTO. Pure function => trivial to unit-test."""
    first, last = raw.get("FirstName") or "", raw.get("LastName") or ""
    return CustomerProfile(
        crm_id=str(raw["ContactId"]),
        email=raw["EmailAddress"].lower(),
        full_name=f"{first} {last}".strip() or raw["EmailAddress"],
        tier=(raw.get("LoyaltyTier") or "standard").lower(),
    )


def from_profile_input(email: str, full_name: str | None) -> dict:
    first, _, last = (full_name or "").partition(" ")
    return {"EmailAddress": email, "FirstName": first, "LastName": last}


class CRMConnector(BaseHttpConnector):
    name = "crm"

    @classmethod
    def from_settings(cls, s: Settings, **overrides) -> CRMConnector:
        return cls(s.crm_base_url, auth=ApiKeyAuth(s.crm_api_key.get_secret_value(), "X-CRM-Key"),
                   timeout=s.http_timeout_seconds,
                   retry=RetryPolicy(s.retry_max_attempts, s.retry_base_delay),
                   breaker=CircuitBreaker(s.breaker_failure_threshold, s.breaker_reset_seconds),
                   **overrides)

    async def upsert_customer(self, *, email: str, full_name: str | None = None) -> CustomerProfile:
        try:
            # PUT is idempotent by definition => safely retried automatically.
            resp = await self.request("PUT", "/contacts", json=from_profile_input(email, full_name))
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"CRM unavailable: {exc}") from exc
        return to_profile(resp.json())

    async def get_customer(self, email: str) -> CustomerProfile | None:
        try:
            resp = await self.request("GET", "/contacts", params={"email": email})
        except ConnectorHTTPError as exc:
            if exc.status == 404:
                return None
            raise UpstreamUnavailableError(f"CRM error {exc.status}") from exc
        except ConnectorError as exc:
            raise UpstreamUnavailableError(f"CRM unavailable: {exc}") from exc
        return to_profile(resp.json())
