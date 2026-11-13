"""Simulated partner APIs: payment (OAuth2), CRM (API key), shipping (API key).

One tiny FastAPI app hosting three fake vendors so the whole system runs offline
under Docker Compose. It deliberately misbehaves on demand so you can SEE the
resilience code work:

  amount 1300 cents           -> payment declined (402)
  amount 1399 cents           -> payment API returns 503 on the first attempt per
                                 idempotency key, then succeeds (retry demo)
  tokens expire after 60s     -> forces the connector's refresh path
  CRM email containing 'down' -> CRM returns 503 (graceful-degradation demo)
"""
from __future__ import annotations

import secrets
import time
import uuid

from fastapi import APIRouter, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock partners")
TOKEN_TTL = 60
_tokens: dict[str, float] = {}
_charges: dict[str, dict] = {}  # idempotency key -> charge
_flaky_seen: set[str] = set()
_contacts: dict[str, dict] = {}

# ---------------------------------------------------------------- payment
payment = APIRouter(prefix="/payment")


@payment.post("/oauth/token")
async def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)):
    if grant_type != "client_credentials" or client_secret != "payment-dev-secret":
        raise HTTPException(401, "invalid_client")
    t = secrets.token_urlsafe(24)
    _tokens[t] = time.time() + TOKEN_TTL
    return {"access_token": t, "token_type": "Bearer", "expires_in": TOKEN_TTL}


def _check_bearer(authorization: str | None) -> None:
    t = (authorization or "").removeprefix("Bearer ")
    if _tokens.get(t, 0) < time.time():
        raise HTTPException(401, "token expired or invalid")


@payment.post("/v1/charges")
async def charge(request: Request, authorization: str | None = Header(None), idempotency_key: str | None = Header(None)):
    _check_bearer(authorization)
    body = await request.json()
    key = idempotency_key or uuid.uuid4().hex
    if key in _charges:  # idempotent replay -> same charge, no double billing
        return _charges[key]
    amount = body["amount"]
    if amount == 1300:
        return JSONResponse({"error": {"code": "card_declined"}}, 402)
    if amount == 1399 and key not in _flaky_seen:
        _flaky_seen.add(key)
        return JSONResponse({"error": {"code": "temporarily_unavailable"}}, 503)
    _charges[key] = {"id": f"ch_{uuid.uuid4().hex[:14]}", "amount": amount, "currency": body["currency"],
                     "status": "succeeded"}
    return _charges[key]


@payment.post("/v1/refunds")
async def refund(request: Request, authorization: str | None = Header(None)):
    _check_bearer(authorization)
    body = await request.json()
    return {"id": f"re_{uuid.uuid4().hex[:14]}", "charge": body["charge"], "status": "succeeded"}


# ---------------------------------------------------------------- CRM (PascalCase on purpose)
crm = APIRouter(prefix="/crm")


def _crm_key(key: str | None) -> None:
    if key != "crm-dev-key":
        raise HTTPException(401, "bad api key")


@crm.put("/contacts")
async def upsert_contact(request: Request, x_crm_key: str | None = Header(None)):
    _crm_key(x_crm_key)
    body = await request.json()
    email = body["EmailAddress"].lower()
    if "down" in email:
        raise HTTPException(503, "CRM maintenance")
    c = _contacts.setdefault(email, {"ContactId": 1000 + len(_contacts), "EmailAddress": email,
                                     "LoyaltyTier": "GOLD" if len(email) % 2 else "STANDARD"})
    c.update({k: v for k, v in body.items() if v})
    return c


@crm.get("/contacts")
async def get_contact(email: str, x_crm_key: str | None = Header(None)):
    _crm_key(x_crm_key)
    if email.lower() not in _contacts:
        raise HTTPException(404, "no such contact")
    return _contacts[email.lower()]


# ---------------------------------------------------------------- shipping
shipping = APIRouter(prefix="/shipping")


@shipping.get("/rates")
async def rates(zip: str = Query(...), kg: float = Query(...), x_api_key: str | None = Header(None)):
    if x_api_key != "shipping-dev-key":
        raise HTTPException(401, "bad api key")
    base = 4.5 + kg * 1.25
    return {"rates": [{"carrier": "ACME Ground", "price": f"{base:.2f}", "eta_days": 5},
                      {"carrier": "ACME Express", "price": f"{base * 2.1:.2f}", "eta_days": 2}]}


for r in (payment, crm, shipping):
    app.include_router(r)


@app.get("/health")
async def health():
    return {"status": "ok"}
