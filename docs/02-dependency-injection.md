# Chapter 2: Dependency Injection

> **Builds on:** [Chapter 1](01-fastapi.md). **Next:** [Chapter 3: Backend architecture](03-backend-architecture.md).
> **Code:** [app/api/deps.py](../app/api/deps.py), [app/container.py](../app/container.py), [app/services/factory.py](../app/services/factory.py), [app/mcp_server/server.py](../app/mcp_server/server.py) · **Tests:** [tests/test_02_dependency_injection.py](../tests/test_02_dependency_injection.py)

---

## 2.1 Inversion of Control and Dependency Injection

**Concept.** *Without* DI an object builds what it needs:
```python
class OrderService:
    def __init__(self):
        self.payment = StripeClient(os.environ["KEY"])   # hard-wired
```
*With* DI it **receives** collaborators from outside:
```python
class OrderService:
    def __init__(self, payment: PaymentPort): self.payment = payment
```
**Inversion of Control** is the principle ("the framework/caller decides what I get, not me"); **DI** is the technique (pass dependencies in).

**Why it exists.** Hard-wired dependencies make code untestable (you'd call the real payment API in tests), inflexible (swap vendors = edit services) and hide what a class needs.

**Flow.** One *composition root* builds concrete objects and hands them down: `Container` → providers (`Depends`) → `OrderService(session, repos, payment, crm)`.

**In this project.** [app/container.py](../app/container.py) is the composition root; `OrderService` depends only on **ports** (`PaymentPort`, `CRMPort` - `typing.Protocol`s), never on `PaymentConnector`.

**Mistakes.** Service locator (`get_container()` called *inside* services - hides dependencies; we allow it only at MCP provider edges); injecting *everything* (a 12-parameter constructor says the class does too much); global singletons mutated by tests.

**Trade-offs.** Manual constructor injection (ours) vs DI frameworks (`dependency-injector`, `punq`). FastAPI's `Depends` is request-scoped DI built in; for the rest, plain constructors + one composition root are usually enough in Python.

**Test.** `test_external_client_injected_as_fake`.

---

## 2.2 FastAPI `Depends` and dependency resolution

**Concept.** Declare what an endpoint needs as parameters with `Depends(provider)`. FastAPI calls the provider and passes the result.

**Flow.** At startup FastAPI inspects each endpoint and builds a **dependency graph**. Per request it resolves leaves first, caches each provider's result, then calls the endpoint. For `POST /orders`:

```
create_order
 ├─ svc  = get_order_service ─┬─ session = get_session ── container = get_container
 │                            └─ container (cached: same object)
 ├─ user = get_current_principal ─┬─ token = oauth2_scheme
 │                                └─ settings = get_settings ── container (cached)
 └─ background (injected by type)
```

**Minimal example.**
```python
def get_settings(): return Settings()
@app.get("/info")
def info(s: Annotated[Settings, Depends(get_settings)]): return s.app_name
```

**In this project.** [deps.py](../app/api/deps.py). Using `Annotated` aliases (`CurrentUser`, `OrderServiceDep`) keeps endpoint signatures short and reusable.

**Mistakes.** Calling the provider yourself (`Depends(get_x())` - note the parentheses); doing heavy work in providers that every route pays for; circular dependencies.

**Test.** `test_dependency_cache_nested_and_yield_cleanup_order` (self-contained demo), `test_authentication_dependency`.

---

## 2.3 Nested dependencies and dependency caching

**Concept.** Providers can themselves declare `Depends`. Within **one request**, the same provider is called **once** and its result is reused (`use_cache=True`, the default).

**Why.** `get_session` is needed by both `get_order_service` and `get_inventory_service`; they must share *one* session/transaction or you get two transactions that can't see each other's uncommitted work.

**Flow.** Cache key = the provider function. `Depends(fn, use_cache=False)` forces a fresh call.

**In this project.** The mini-app in `test_dependency_cache_nested_and_yield_cleanup_order` proves `config` and `session` are built **once** although requested several times. `test_use_cache_false_calls_dependency_each_time` shows the opt-out.

**Mistakes.** Assuming cache is *across* requests (it's per request); putting a mutable per-request object behind `use_cache=False` and being surprised it's two objects.

**Trade-off.** Cache-per-request is the right default; for process-wide singletons use the lifespan/`app.state` (our `Container`) or `functools.lru_cache` (`get_settings`).

---

## 2.4 `yield` dependencies and resource cleanup

**Concept.** A provider that `yield`s is a context manager: code before `yield` = setup, after = teardown, even on errors.

**Why.** Resources that must be released (DB sessions, files, locks) need guaranteed cleanup.

**Flow.** setup → endpoint runs → teardown in **reverse** order of setup.

**Minimal example (this is our real session provider).**
```python
async def get_session(c: Container = Depends(get_container)):
    async with session_scope(c.sessionmaker) as session:   # commit on success, rollback on error
        yield session
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
```

**Important subtlety.** In modern FastAPI the teardown of a `yield` dependency runs **after the response has been sent** by default. For a DB session that means the `COMMIT` happens *after* the client got `201`; a failed commit would be invisible. `scope="function"` makes teardown run when the endpoint returns, **before** the response. We use it for the session.

**Mistakes.** Catching exceptions around `yield` and swallowing them (breaks rollback); using the session in a `BackgroundTask` after it was closed (open a new one inside the task).

**MCP twist.** `fastmcp.dependencies.Depends` mirrors this, but wants an **async context manager**: decorate the generator with `@asynccontextmanager` ([server.py](../app/mcp_server/server.py)). Bare async generators were *not* entered (we hit this bug; see the test history).

**Test.** `test_session_dependency_commits_on_success_and_rolls_back_on_error`.

---

## 2.5 Database session injection

**Concept.** The session (unit of work) is created per request by a provider and injected into repositories → services. Nothing below the provider ever opens or closes sessions or commits.

**Why.** One request = one transaction = one atomic outcome. If payment fails, the stock reservation and the order row vanish together.

**Flow.** `get_session` → `build_order_service(session, container)` → repositories get the same session → `flush()` inside, `commit()` in teardown.

**In this project.** [app/services/factory.py](../app/services/factory.py) is the *single* wiring recipe used by both REST (`get_order_service`) and MCP (`get_order_service` in server.py), so the two interfaces cannot diverge.

**Mistakes.** `commit()` inside repositories (can't compose operations atomically); sharing a session across concurrent tasks; global `scoped_session` in async code.

**Test.** `test_override_session_dependency_to_prove_service_uses_injected_session`, `test_transaction_rollback_on_payment_failure_leaves_no_trace`.

---

## 2.6 Authentication and authorization dependencies

**Authentication** = who are you? **Authorization** = what may you do?

**Flow.** `OAuth2PasswordBearer` extracts the `Authorization: Bearer …` token → `get_current_principal` verifies signature, expiry, issuer, audience (`algorithms=[...]` pinned) → returns a `Principal(user_id, email, role)` → `RoleChecker` compares role → endpoint runs. Data-level rules (customers see only their orders) live in the service.

**In this project.** `CurrentUser`, `require_staff`, `require_admin` in [deps.py](../app/api/deps.py); token logic in [security.py](../app/core/security.py). Router-level guard: `APIRouter(dependencies=[Depends(require_admin)])`.

**Mistakes.** Checking roles in the frontend only; accepting `alg: none`/algorithm confusion (we pin `HS256`); long-lived tokens with no revocation story (we use 30 min; add refresh tokens + a deny-list when needed); returning 403 where 404 avoids leaking existence.

**Trade-offs.** Stateless JWT (no DB hit, can't revoke instantly) vs opaque tokens/sessions (instant revoke, DB/Redis lookup per request).

**Test.** `test_login_flow_and_token_use`, `test_class_based_authorization_dependency`.

---

## 2.7 Class-based dependencies

**Concept.** Any *callable* can be a dependency. A class with `__call__` lets you **configure** a dependency per use site.

```python
class RoleChecker:
    def __init__(self, *allowed: Role): self.allowed = set(allowed)
    def __call__(self, user: CurrentUser) -> Principal:
        if user.role not in self.allowed: raise PermissionDeniedError(...)
        return user

require_staff = RoleChecker(Role.ADMIN, Role.STAFF)     # created once at import
@router.patch("/x", dependencies=[Depends(require_staff)])
```

**Why.** A function dependency can't take constructor arguments without closures; a class gives named, testable, reusable "policies".

**Flow.** `__init__` runs once (configuration); `__call__` runs per request and *its* parameters are resolved by DI.

**In this project.** `RoleChecker`; `OAuth2PasswordRequestForm` (a class used as `Depends()`).

**Test.** `test_role_checker_is_parameterised_per_instance`.

---

## 2.8 Dependency overrides and mocking in pytest

**Concept.** `app.dependency_overrides[original] = replacement` swaps a provider for the whole app during a test.

**Why.** This is *the* payoff of DI: replace the DB, auth or a vendor without monkey-patching internals.

**Flow.** Resolution looks up the override map before calling the provider; overrides themselves can have dependencies.

**Minimal example.**
```python
app.dependency_overrides[get_current_principal] = lambda: Principal(1, "x@y.z", Role.ADMIN)
try:
    ...
finally:
    app.dependency_overrides.clear()      # always clean up!
```

**In this project.** Two levels: (1) override *providers* (`test_dependency_override_swaps_collaborators_for_tests`); (2) inject **fake ports** through the container ([tests/conftest.py](../tests/conftest.py): `FakePayment`, `FakeCRM`, `FakeShipping`, `ScriptedLLM`). Prefer (2) for business tests: it exercises more real code.

**Mistakes.** Forgetting to clear overrides (test pollution); overriding so much that the test proves nothing; mocking the thing you're testing.

**Test.** `test_dependency_override_swaps_collaborators_for_tests`.

---

## 2.9 Injecting external API clients and services

**Concept.** Vendor clients are dependencies like any other: defined by a **port** (what we need), implemented by an **adapter** (how the vendor does it), injected.

**Flow.** `Container` builds `PaymentConnector.from_settings(settings)`; `OrderService` calls `self.payment.charge(...)`; tests inject `FakePayment`.

**Why long-lived clients.** `httpx.AsyncClient` pools connections; creating one per request wastes TCP/TLS handshakes. Create at startup (container), close at shutdown.

**In this project.** Same pattern for payment, CRM, shipping, reporting, the LLM (`LLMClient`) and MCP servers (`McpServerConnector`). See Chapter 6.

**Mistakes.** Constructing clients inside request handlers; reading env vars in services; no way to substitute in tests.

**Test.** `test_external_client_injected_as_fake`; [tests/test_06_connectors.py](../tests/test_06_connectors.py).

---

## Common mistakes cheat-sheet
1. `Depends(fn())` instead of `Depends(fn)`. 2. Commit inside repositories. 3. Session shared by concurrent tasks. 4. Teardown after response for commits (use `scope="function"`). 5. Leaving overrides set. 6. Service locator in business code. 7. Global mutable singletons. 8. Bare async-generator dependencies in fastmcp.

## Interview questions (scenario-based)

**Q1. A teammate calls `get_container()` inside `OrderService.create_order`. What's wrong and how do you refactor?**
*A.* It's a service locator: hidden dependency, can't substitute in unit tests without patching a global. Add the needed collaborator to the constructor typed as a Protocol, build the service in a provider/factory (`build_order_service`), and have tests pass fakes.

**Q2. After a successful POST, a follow-up GET sometimes doesn't see the new row. Cause?**
*A.* The `yield` dependency commits in teardown, which by default runs *after* the response is sent, so a fast client races the commit. Use `Depends(get_session, scope="function")` to commit before responding (or commit explicitly in the endpoint).

**Q3. Two dependencies both need the DB session; they behave as if in different transactions. Why?**
*A.* Probably `use_cache=False`, or the session is constructed inside each dependency instead of being a shared provider. Make one `get_session` provider and depend on it (per-request cache ensures the same instance).

**Q4. How do you test an endpoint that needs an admin without creating users/tokens?**
*A.* Override `get_current_principal` in `dependency_overrides` (clear it in `finally`). For auth *logic* itself, don't override: sign a real JWT with the test secret.

**Q5. Why Protocols for `PaymentPort` instead of an ABC or concrete class?**
*A.* Structural typing: fakes and adapters satisfy the port without inheriting, so test doubles stay trivial and the domain doesn't import the connector package. ABCs add registration/inheritance ceremony; concrete types create coupling.

**Q6. You need a per-tenant database. Where does that selection happen?**
*A.* In the session provider: derive tenant from the token/host, pick the right engine/schema (`search_path`) and yield a session bound to it. Services and repositories remain unchanged: that is the point of injection.

**Q7. `get_settings()` is called on every request and parses env every time. Fix?**
*A.* `@lru_cache` (process singleton) - and `get_settings.cache_clear()` in tests that change env. Or load once in lifespan and store on `app.state`.

**Q8. A dependency opens a file/lock and the endpoint raises. Is it released?**
*A.* Yes if it's a `yield` dependency with `try/finally` (or `async with`): teardown runs on exceptions. Don't swallow exceptions around the `yield` unless you intend to hide them.

**Q9. What breaks if you inject the DB *session* into a long-lived singleton service?**
*A.* Sessions are not thread/task safe and hold a transaction; the singleton would share state across requests and leak connections. Inject the **sessionmaker** (factory) into singletons, and create sessions per unit of work.

**Q10. Same business logic is exposed over REST and MCP. How do you avoid two wiring codepaths drifting?**
*A.* One factory (`build_order_service`) used by both frameworks' provider layers; transports contain no business logic and only translate auth + errors. A test calls the service directly (no HTTP) to prove independence.
