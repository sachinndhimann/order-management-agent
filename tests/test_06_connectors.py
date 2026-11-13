"""Chapter 6 - Connectors: adapters, auth, token refresh, retries/backoff, circuit breaker, transformation, secrets, mocking."""
import asyncio

import httpx
import pytest
import respx
from fastmcp import FastMCP

from app.connectors.base import (
    ApiKeyAuth, BaseHttpConnector, BearerAuth, BreakerState, CircuitBreaker, CircuitOpenError, ConnectorAuthError,
    ConnectorHTTPError, ConnectorTimeout, ConnectorUnavailable, OAuth2ClientCredentials, RetryPolicy,
)
from app.connectors.crm import CRMConnector, to_profile
from app.connectors.mcp_external import McpServerConnector
from app.connectors.payment import PaymentConnector, PaymentStatus
from app.connectors.shipping import ShippingConnector
from app.core.config import Settings, get_settings
from app.core.errors import PaymentFailedError, UpstreamUnavailableError
from external_mcp.logistics_server import create_logistics_server
from mock_services.app import app as mock_app

BASE = "http://vendor.test"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []

    async def fake_sleep(d):
        sleeps.append(d)  # record instead of sleeping => instant tests
    kw.setdefault("retry", RetryPolicy(max_attempts=3, base_delay=0.2, jitter=False))
    return BaseHttpConnector(BASE, sleep=fake_sleep, **kw), sleeps


# ------------------------------------------------------------------ retries + exponential backoff
@respx.mock
async def test_retries_transient_503_with_exponential_backoff():
    route = respx.get(f"{BASE}/x").mock(side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    c, sleeps = make()
    assert (await c.request("GET", "/x")).json() == {"ok": 1}
    assert route.call_count == 3 and sleeps == [0.2, 0.4]  # doubles each attempt


@respx.mock
async def test_gives_up_after_max_attempts():
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(503))
    c, sleeps = make()
    with pytest.raises(ConnectorUnavailable):
        await c.request("GET", "/x")
    assert len(sleeps) == 2


@respx.mock
async def test_post_is_not_retried_unless_marked_idempotent():
    route = respx.post(f"{BASE}/charge").mock(return_value=httpx.Response(503))
    c, _ = make()
    with pytest.raises(ConnectorUnavailable):
        await c.request("POST", "/charge", json={})
    assert route.call_count == 1      # a blind retry could double-charge
    route.reset()
    with pytest.raises(ConnectorUnavailable):
        await c.request("POST", "/charge", json={}, idempotent=True)
    assert route.call_count == 3


@respx.mock
async def test_client_errors_are_not_retried_and_do_not_trip_breaker():
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(404, text="nope"))
    c, _ = make(breaker=CircuitBreaker(failure_threshold=1))
    with pytest.raises(ConnectorHTTPError) as e:
        await c.request("GET", "/x")
    assert e.value.status == 404 and route.call_count == 1 and c.breaker.state is BreakerState.CLOSED


@respx.mock
async def test_timeouts_are_retried_then_raised_as_connector_timeout():
    respx.get(f"{BASE}/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    c, sleeps = make()
    with pytest.raises(ConnectorTimeout):
        await c.request("GET", "/slow")
    assert len(sleeps) == 2


def test_full_jitter_stays_within_ceiling():
    p = RetryPolicy(base_delay=1.0, max_delay=4.0, jitter=True)
    assert all(0 <= p.delay(n) <= min(4.0, 2 ** (n - 1)) for n in (1, 2, 3, 4, 5) for _ in range(50))
    assert RetryPolicy(base_delay=1.0, max_delay=4.0, jitter=False).delay(10) == 4.0  # capped


# ------------------------------------------------------------------ circuit breaker
@respx.mock
async def test_circuit_breaker_opens_fails_fast_then_recovers():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=clock)
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(503))
    c, _ = make(breaker=breaker, retry=RetryPolicy(max_attempts=1))
    for _ in range(2):
        with pytest.raises(ConnectorUnavailable):
            await c.request("GET", "/x")
    assert breaker.state is BreakerState.OPEN
    calls = route.call_count
    with pytest.raises(CircuitOpenError):
        await c.request("GET", "/x")
    assert route.call_count == calls                        # fail fast: vendor NOT contacted

    clock.t += 31                                           # cool-down elapsed => one probe allowed
    route.mock(return_value=httpx.Response(200, json={}))
    await c.request("GET", "/x")
    assert breaker.state is BreakerState.CLOSED


@respx.mock
async def test_half_open_probe_failure_reopens():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(503))
    c, _ = make(breaker=b, retry=RetryPolicy(max_attempts=1))
    with pytest.raises(ConnectorUnavailable):
        await c.request("GET", "/x")
    clock.t += 11
    with pytest.raises(ConnectorUnavailable):
        await c.request("GET", "/x")
    assert b.state is BreakerState.OPEN


# ------------------------------------------------------------------ auth strategies
@respx.mock
async def test_api_key_and_bearer_headers():
    r = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    c1, _ = make(auth=ApiKeyAuth("k1", "X-Key"))
    c2, _ = make(auth=BearerAuth("tok"))
    await c1.request("GET", "/x")
    assert r.calls.last.request.headers["X-Key"] == "k1"
    await c2.request("GET", "/x")
    assert r.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_oauth2_token_is_cached_and_refreshed_before_expiry():
    clock = Clock()
    token_route = respx.post(f"{BASE}/token").mock(side_effect=lambda req: httpx.Response(
        200, json={"access_token": f"t{token_route.call_count}", "expires_in": 100}))
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    auth = OAuth2ClientCredentials(f"{BASE}/token", "id", "secret", skew=30, clock=clock)
    c, _ = make(auth=auth)
    await c.request("GET", "/x")
    await c.request("GET", "/x")
    assert token_route.call_count == 1                      # cached
    clock.t += 71                                           # inside the 30s safety window (expires at +100)
    await c.request("GET", "/x")
    assert token_route.call_count == 2                      # proactively refreshed


@respx.mock
async def test_401_triggers_one_token_refresh_and_retry():
    respx.post(f"{BASE}/token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "old", "expires_in": 999}),
        httpx.Response(200, json={"access_token": "new", "expires_in": 999})])
    api = respx.get(f"{BASE}/x").mock(side_effect=lambda req: httpx.Response(
        200 if req.headers["Authorization"] == "Bearer new" else 401))
    auth = OAuth2ClientCredentials(f"{BASE}/token", "id", "secret")
    c, _ = make(auth=auth)
    assert (await c.request("GET", "/x")).status_code == 200
    assert api.call_count == 2 and auth.fetch_count == 2    # server revoked token => we recovered


@respx.mock
async def test_persistent_401_raises_auth_error_without_looping():
    respx.post(f"{BASE}/token").mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 999}))
    api = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(401))
    c, _ = make(auth=OAuth2ClientCredentials(f"{BASE}/token", "id", "bad"))
    with pytest.raises(ConnectorAuthError):
        await c.request("GET", "/x")
    assert api.call_count == 2


@respx.mock
async def test_concurrent_callers_share_a_single_token_fetch():
    t = respx.post(f"{BASE}/token").mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 999}))
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    c, _ = make(auth=OAuth2ClientCredentials(f"{BASE}/token", "id", "s"))
    await asyncio.gather(*[c.request("GET", "/x") for _ in range(10)])
    assert t.call_count == 1                                # lock prevents a token stampede


@respx.mock
async def test_token_endpoint_failure_is_auth_error():
    respx.post(f"{BASE}/token").mock(return_value=httpx.Response(500))
    c, _ = make(auth=OAuth2ClientCredentials(f"{BASE}/token", "id", "s"))
    with pytest.raises(ConnectorAuthError):
        await c.request("GET", "/x")


# ------------------------------------------------------------------ schema transformation (adapters)
def test_crm_schema_transformation_pure_function():
    p = to_profile({"ContactId": 7, "FirstName": "Ada", "LastName": "Lovelace", "EmailAddress": "ADA@X.COM", "LoyaltyTier": "GOLD"})
    assert (p.crm_id, p.email, p.full_name, p.tier) == ("7", "ada@x.com", "Ada Lovelace", "gold")
    assert to_profile({"ContactId": 1, "EmailAddress": "z@x.com"}).full_name == "z@x.com"


@respx.mock
async def test_shipping_picks_cheapest_and_converts_money_exactly():
    respx.get("http://ship.test/rates").mock(return_value=httpx.Response(200, json={"rates": [
        {"carrier": "Fast", "price": "19.99", "eta_days": 1}, {"carrier": "Slow", "price": "4.10", "eta_days": 6}]}))
    c = ShippingConnector("http://ship.test", auth=ApiKeyAuth("k"))
    q = await c.quote(postal_code="94105", weight_kg=2)
    assert (q.carrier, q.cost_cents, q.eta_days) == ("Slow", 410, 6)   # Decimal, not float: 4.10*100 != 409.99..


@respx.mock
async def test_crm_404_is_none_and_503_is_domain_error():
    c = CRMConnector("http://crm.test", auth=ApiKeyAuth("k"), retry=RetryPolicy(1), sleep=lambda d: asyncio.sleep(0))
    respx.get("http://crm.test/contacts").mock(return_value=httpx.Response(404))
    assert await c.get_customer("x@y.z") is None
    respx.get("http://crm.test/contacts").mock(return_value=httpx.Response(503))
    with pytest.raises(UpstreamUnavailableError):
        await c.get_customer("x@y.z")


# ------------------------------------------------------------------ payment connector against the simulated vendor (ASGI, no sockets)
@pytest.fixture
async def payment():
    s = Settings(payment_base_url="http://mock/payment", _env_file=None)
    t = httpx.ASGITransport(app=mock_app)

    async def no_sleep(d): ...
    p = PaymentConnector.from_settings(s, transport=t, sleep=no_sleep)
    yield p
    await p.aclose()


async def test_payment_happy_path_transforms_vendor_json(payment):
    r = await payment.charge(order_id="o1", amount_cents=2500, currency="USD", customer_email="a@b.co")
    assert r.status is PaymentStatus.SUCCEEDED and r.reference.startswith("ch_") and r.amount_cents == 2500


async def test_payment_idempotency_key_prevents_double_charge(payment):
    a = await payment.charge(order_id="same", amount_cents=500, currency="usd", customer_email="a@b.co")
    b = await payment.charge(order_id="same", amount_cents=500, currency="usd", customer_email="a@b.co")
    assert a.reference == b.reference


async def test_payment_decline_is_business_error_not_outage(payment):
    with pytest.raises(PaymentFailedError):
        await payment.charge(order_id="o2", amount_cents=1300, currency="usd", customer_email="a@b.co")
    assert payment.breaker.state is BreakerState.CLOSED


async def test_payment_transient_503_is_retried_transparently(payment):
    r = await payment.charge(order_id="flaky-1", amount_cents=1399, currency="usd", customer_email="a@b.co")
    assert r.status is PaymentStatus.SUCCEEDED               # first attempt 503, second OK


async def test_payment_refund_and_expired_token_recovery(payment):
    r = await payment.charge(order_id="o3", amount_cents=100, currency="usd", customer_email="a@b.co")
    from mock_services import app as vendor
    vendor._tokens.clear()                                   # vendor forgets our token (revocation/expiry)
    refund_id = await payment.refund(payment_ref=r.reference, amount_cents=100)
    assert refund_id.startswith("re_") and payment.auth.fetch_count == 2


async def test_crm_against_simulated_vendor_and_degradation():
    s = Settings(crm_base_url="http://mock/crm", _env_file=None)
    crm = CRMConnector.from_settings(s, transport=httpx.ASGITransport(app=mock_app), sleep=lambda d: asyncio.sleep(0))
    p = await crm.upsert_customer(email="grace@example.com", full_name="Grace Hopper")
    assert p.full_name == "Grace Hopper" and p.tier in {"gold", "standard"}
    assert (await crm.get_customer("grace@example.com")).crm_id == p.crm_id
    with pytest.raises(UpstreamUnavailableError):
        await crm.upsert_customer(email="crm-down@example.com")
    await crm.aclose()


# ------------------------------------------------------------------ external MCP server connector
async def test_external_mcp_discovery_and_invocation():
    server = create_logistics_server("k")
    async with McpServerConnector("logistics", server) as conn:
        specs = {t.name: t for t in await conn.list_tools()}
        assert set(specs) == {"track_shipment", "estimate_delivery"}
        assert specs["track_shipment"].input_schema["properties"]["tracking_number"]["pattern"]
        ok = await conn.call_tool("estimate_delivery", {"postal_code": "94105"})
        assert not ok.is_error and ok.data["postal_code"] == "94105"
        bad = await conn.call_tool("track_shipment", {"tracking_number": "AC12345670"})
        assert bad.is_error and "not found" in bad.text.lower()      # tool failure = data, not a crash
        invalid = await conn.call_tool("track_shipment", {"tracking_number": "bad"})
        assert invalid.is_error                                      # schema violation reported to the model


async def test_external_mcp_connector_retries_then_trips_breaker():
    attempts = []
    conn = McpServerConnector("x", FastMCP("x"), retry=RetryPolicy(3, 0.0, jitter=False),
                              breaker=CircuitBreaker(failure_threshold=3), sleep=lambda d: asyncio.sleep(0))

    async def failing():
        attempts.append(1)
        raise TimeoutError

    with pytest.raises(ConnectorTimeout):
        await conn._guarded(failing)
    assert len(attempts) == 3 and conn._breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await conn._guarded(failing)
    assert len(attempts) == 3


# ------------------------------------------------------------------ secret management
def test_secrets_are_masked_and_loadable_from_files(tmp_path, monkeypatch):
    f = tmp_path / "jwt"
    f.write_text("file-secret-value\n")
    monkeypatch.setenv("JWT_SECRET", "placeholder")  # registers cleanup: _apply_file_secrets writes os.environ
    monkeypatch.setenv("JWT_SECRET_FILE", str(f))
    get_settings.cache_clear()
    s = get_settings()
    assert s.jwt_secret.get_secret_value() == "file-secret-value"
    assert "file-secret-value" not in repr(s) and "file-secret-value" not in str(s.jwt_secret)
    get_settings.cache_clear()
