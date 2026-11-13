"""Chapter 1 - FastAPI: routing, parameters, validation, errors, middleware, CORS, background tasks, OpenAPI."""
from app.api.v1 import routers
from tests.conftest import ORDER_BODY


async def test_path_query_header_and_body_parameters(client, auth):
    # body + header
    r = await client.post("/api/v1/orders", json=ORDER_BODY, headers={**auth(), "Idempotency-Key": "key-12345678"})
    assert r.status_code == 201                      # POST create => 201 Created
    assert r.headers["Location"] == f"/api/v1/orders/{r.json()['id']}"
    oid = r.json()["id"]
    # path parameter
    assert (await client.get(f"/api/v1/orders/{oid}", headers=auth())).json()["id"] == oid
    # query parameters: filter + pagination + sort
    lst = await client.get("/api/v1/orders?status=paid&limit=5&offset=0&sort=-total_cents", headers=auth())
    assert lst.status_code == 200 and lst.json()["total"] == 1 and lst.json()["limit"] == 5


async def test_pydantic_validation_returns_422_with_clean_envelope(client, auth):
    bad = {"customer_email": "not-an-email", "items": [{"sku": "KB-100", "quantity": 0}]}
    r = await client.post("/api/v1/orders", json=bad, headers=auth())
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "request_validation_failed"
    locs = {e["loc"] for e in err["details"]["errors"]}
    assert "body.customer_email" in locs and "body.items.0.quantity" in locs
    assert err["request_id"] != "-"


async def test_duplicate_skus_rejected_by_custom_validator(client, auth):
    body = {"customer_email": "ada@example.com", "items": [{"sku": "kb-100", "quantity": 1}, {"sku": "KB-100", "quantity": 1}]}
    r = await client.post("/api/v1/orders", json=body, headers=auth())
    assert r.status_code == 422  # sku normalised to upper-case, then duplicates detected


async def test_response_model_never_leaks_internal_fields(client, auth):
    me = (await client.get("/api/v1/auth/me", headers=auth())).json()
    assert set(me) == {"id", "email", "role"} and "password_hash" not in me


async def test_domain_errors_map_to_status_codes(client, auth):
    unknown = await client.get("/api/v1/orders/" + "0" * 36, headers=auth())
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    huge = {"customer_email": "ada@example.com", "items": [{"sku": "HD-400", "quantity": 99}]}
    r = await client.post("/api/v1/orders", json=huge, headers=auth())
    assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_stock"
    assert (await client.get("/api/v1/orders")).status_code == 401  # no token


async def test_unhandled_exception_hides_internals(client, auth, container):
    async def boom(*a, **k):
        raise RuntimeError("secret db password leaked?!")
    container.payment.charge = boom
    r = await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())
    assert r.status_code == 500 and "secret" not in r.text
    assert r.json()["error"]["code"] == "internal_error"


async def test_middleware_adds_request_id_and_honours_incoming_one(client):
    r = await client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert r.headers["X-Request-ID"] == "trace-abc"
    assert "X-Request-ID" in (await client.get("/health")).headers


async def test_cors_preflight(client):
    r = await client.options("/api/v1/orders", headers={"Origin": "http://localhost:3000",
                             "Access-Control-Request-Method": "POST"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    bad = await client.options("/api/v1/orders", headers={"Origin": "http://evil.example",
                               "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


async def test_background_task_runs_after_response(client, auth, monkeypatch):
    seen = []

    async def fake_notify(order_id, email):
        seen.append((order_id, email))
    monkeypatch.setattr(routers, "notify_customer", fake_notify)
    r = await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())
    assert r.status_code == 201 and seen == [(r.json()["id"], "ada@example.com")]


async def test_async_concurrency_enrichment_degrades_gracefully(client, auth, container):
    oid = (await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())).json()["id"]
    ok = (await client.get(f"/api/v1/orders/{oid}/enriched", headers=auth())).json()
    assert ok["customer_tier"] == "gold" and ok["shipping_eta_days"] == 3 and ok["degraded"] == []
    container.crm.down = True
    degraded = (await client.get(f"/api/v1/orders/{oid}/enriched", headers=auth())).json()
    assert degraded["degraded"] == ["crm"] and degraded["shipping_eta_days"] == 3  # still 200


async def test_openapi_and_versioning(client):
    spec = (await client.get("/openapi.json")).json()
    assert spec["info"]["title"] and "/api/v1/orders" in spec["paths"] and "/api/v2/orders/{order_id}" in spec["paths"]
    assert "post" in spec["paths"]["/api/v1/orders"]
    assert "201" in spec["paths"]["/api/v1/orders"]["post"]["responses"]


async def test_v2_changes_shape_but_v1_unchanged(client, auth):
    oid = (await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())).json()["id"]
    v1 = (await client.get(f"/api/v1/orders/{oid}", headers=auth())).json()
    v2 = (await client.get(f"/api/v2/orders/{oid}", headers=auth())).json()
    assert "total_cents" in v1 and v2["total"] == {"amount_cents": v1["total_cents"], "currency": "USD"}


async def test_health_ready_metrics(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ready"}
    assert "http_requests_total" in (await client.get("/metrics")).text
