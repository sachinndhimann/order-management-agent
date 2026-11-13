"""Chapter 2 - Dependency Injection: Depends, caching, nesting, yield cleanup, class-based deps, overrides."""
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI

from app.api.deps import RoleChecker, get_session, require_admin
from app.core.security import Principal, Role
from tests.conftest import ORDER_BODY


# ---- minimal, self-contained demos of the mechanics (then proven on the real app below)
async def test_dependency_cache_nested_and_yield_cleanup_order():
    events: list[str] = []
    mini = FastAPI()

    def config():
        events.append("config")
        return {"dsn": "x"}

    async def session(cfg: Annotated[dict, Depends(config)]):
        events.append("session:open")
        yield f"session({cfg['dsn']})"
        events.append("session:close")  # teardown runs after the endpoint

    def repo(s: Annotated[str, Depends(session)], cfg: Annotated[dict, Depends(config)]):
        return f"repo[{s}]"

    @mini.get("/x")
    async def x(r: Annotated[str, Depends(repo)], s: Annotated[str, Depends(session)]):
        return {"r": r, "s": s}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://t") as c:
        body = (await c.get("/x")).json()
    assert body == {"r": "repo[session(x)]", "s": "session(x)"}
    # cached per request: config & session built ONCE although requested 2-3 times
    assert events == ["config", "session:open", "session:close"]


async def test_use_cache_false_calls_dependency_each_time():
    calls = []
    mini = FastAPI()

    def counter():
        calls.append(1)
        return len(calls)

    @mini.get("/n")
    async def n(a: Annotated[int, Depends(counter)], b: Annotated[int, Depends(counter, use_cache=False)]):
        return [a, b]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://t") as c:
        assert (await c.get("/n")).json() == [1, 2]


# ---- on the real application
async def test_session_dependency_commits_on_success_and_rolls_back_on_error(client, auth, container):
    ok = await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())
    assert ok.status_code == 201
    stock = (await client.get("/api/v1/products/KB-100/stock", headers=auth())).json()["available"]
    assert stock == 23  # 25 - 2: committed

    container.payment.decline = True
    bad = await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())
    assert bad.status_code == 402
    stock_after = (await client.get("/api/v1/products/KB-100/stock", headers=auth())).json()["available"]
    assert stock_after == 23  # rolled back: stock reservation did NOT stick


async def test_authentication_dependency(client, auth):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=auth())).status_code == 200


async def test_login_flow_and_token_use(client):
    r = await client.post("/api/v1/auth/token", data={"username": "staff@example.com", "password": "staff12345"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["role"] == "staff"
    wrong = await client.post("/api/v1/auth/token", data={"username": "staff@example.com", "password": "nope"})
    assert wrong.status_code == 401


async def test_class_based_authorization_dependency(client, auth):
    assert (await client.get("/api/v1/reports/sales", headers=auth(Role.STAFF))).status_code == 403
    assert (await client.get("/api/v1/reports/sales", headers=auth(Role.CUSTOMER))).status_code == 403
    assert (await client.get("/api/v1/reports/sales", headers=auth(Role.ADMIN))).status_code == 200


def test_role_checker_is_parameterised_per_instance():
    only_staff = RoleChecker(Role.STAFF)
    p = Principal(1, "a@b.c", Role.CUSTOMER)
    try:
        only_staff(p)
        raise AssertionError("expected PermissionDeniedError")
    except Exception as e:
        assert getattr(e, "status_code", None) == 403
    assert require_admin.allowed == {Role.ADMIN}


async def test_dependency_override_swaps_collaborators_for_tests(client, auth):
    """The payoff of DI: replace the DB session / any provider with NO monkeypatching of internals."""
    from app.api.deps import get_current_principal

    client.app.dependency_overrides[get_current_principal] = lambda: Principal(99, "robot@example.com", Role.ADMIN)
    try:
        r = await client.get("/api/v1/auth/me")  # no Authorization header at all!
        assert r.status_code != 401
    finally:
        client.app.dependency_overrides.clear()


async def test_override_session_dependency_to_prove_service_uses_injected_session(client, auth, container):
    used = []

    async def tracking_session():
        from app.db.session import session_scope
        async with session_scope(container.sessionmaker) as s:
            used.append(s)
            yield s

    client.app.dependency_overrides[get_session] = tracking_session
    try:
        await client.get("/api/v1/products/KB-100/stock", headers=auth())
    finally:
        client.app.dependency_overrides.clear()
    assert len(used) == 1


async def test_external_client_injected_as_fake(client, auth, container):
    """Connectors are injected through the container: the fake records what the service asked for."""
    await client.post("/api/v1/orders", json=ORDER_BODY, headers=auth())
    assert container.payment.charges[0]["amount"] == 2 * 8999
    assert container.crm.upserts == ["ada@example.com"]
