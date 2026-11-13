# Chapter 1: FastAPI

> **Builds on:** nothing. Start here. **Next:** [Chapter 2: Dependency Injection](02-dependency-injection.md).
> **Code:** [app/main.py](../app/main.py), [app/api/](../app/api/) · **Tests:** [tests/test_01_fastapi.py](../tests/test_01_fastapi.py)

Every topic below uses the same layout: **Concept → Why it exists → Execution flow → Minimal example → In this project → Mistakes & production → Trade-offs → Test**.

---

## 1.1 Application lifecycle, ASGI and Uvicorn

**Concept.** A web server (Uvicorn) speaks HTTP to the internet. Your Python code is an *application*. **ASGI** is the tiny agreed interface between them: the application is an `async` callable `app(scope, receive, send)`. FastAPI is a (large) ASGI app built on Starlette.

**Why it exists.** The older standard (WSGI) was synchronous: one request occupies one thread until done. ASGI is async-native, so one process can hold thousands of mostly-waiting connections (DB, partner APIs, LLM calls), and also supports WebSockets and streaming, which MCP's Streamable HTTP relies on.

**Execution flow.**
1. `uvicorn app.main:get_app --factory` imports the factory and calls it once.
2. Uvicorn sends the ASGI **lifespan startup** event → our `lifespan()` builds the `Container` (DB engine, HTTP client pools).
3. Per request: Uvicorn parses bytes → builds `scope` → calls the middleware stack → router → dependency resolution → your function → response serialization → bytes back.
4. On shutdown: lifespan teardown closes pools (no leaked connections).

**Minimal example.**
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app):
    app.state.db = await connect()      # startup
    yield                                # app serves requests here
    await app.state.db.close()           # shutdown

app = FastAPI(lifespan=lifespan)
```

**In this project.** `create_app()` in [app/main.py](../app/main.py) is an *application factory*; `get_app()` is the Uvicorn entry point. Tests call `create_app(container)` with fakes.

**Mistakes & production.**
- Creating DB engines/HTTP clients at import time → side effects on `import`, impossible to test. Use lifespan.
- **Blocking the event loop** (`time.sleep`, `requests.get`, heavy CPU) freezes *every* request. Use `await`/async libraries, or `def` endpoints (FastAPI runs them in a thread pool).
- Production: `uvicorn --workers N` (or gunicorn+uvicorn workers), one container per core is the usual Kubernetes shape; terminate TLS at a proxy; set `--timeout-graceful-shutdown`.

**Trade-offs.** Module-level `app = FastAPI()` is simpler for tiny apps; the factory costs 3 lines but buys testability. `@app.on_event("startup")` is deprecated in favour of lifespan.

**Test.** `test_health_ready_metrics`; every test that builds `create_app(container)`.

---

## 1.2 Routing, `APIRouter` and endpoint design

**Concept.** A *route* maps `(HTTP method, path)` to a Python function. `APIRouter` is a mini-app of routes you mount under a prefix.

**Why.** One giant file of routes doesn't scale; routers group routes by resource (`/orders`, `/products`), and let you attach shared dependencies/tags/prefixes once.

**Flow.** Router tries path patterns in registration order → first match wins → path params are extracted & converted → dependencies are resolved → function runs.

**Minimal example.**
```python
router = APIRouter(prefix="/orders", tags=["orders"])
@router.get("/{order_id}")
async def get_order(order_id: str): ...
app.include_router(router, prefix="/api/v1")   # final path: /api/v1/orders/{order_id}
```

**In this project.** [app/api/v1/routers.py](../app/api/v1/routers.py) defines `auth_router`, `products_router`, `orders_router`, `reports_router`; [app/main.py](../app/main.py) mounts them under `/api/v1`. `reports_router` uses `dependencies=[Depends(require_admin)]` to guard *every* route at once.

**Mistakes.** Declaring `/orders/{id}` before `/orders/me` makes `me` unreachable (order matters for overlapping patterns). Putting business logic in the route function (see Chapter 3 for why not).

**Trade-offs.** Group by resource (ours) vs by feature/domain package (better in very large codebases).

**Test.** `test_path_query_header_and_body_parameters`.

---

## 1.3 HTTP methods and REST conventions

| Method | Meaning | Safe | Idempotent | Our use |
|---|---|---|---|---|
| GET | read | ✔ | ✔ | `GET /orders/{id}` |
| POST | create / action | ✘ | ✘ (unless you add a key) | `POST /orders` + `Idempotency-Key` |
| PUT | replace whole resource | ✘ | ✔ | CRM `PUT /contacts` (vendor side) |
| PATCH | partial update | ✘ | usually | `PATCH /orders/{id}/status` |
| DELETE | remove | ✘ | ✔ | (not exposed: orders are cancelled, not deleted) |

**Why it matters.** Intermediaries and clients *act* on these semantics: browsers prefetch GETs, retry libraries retry idempotent methods, caches cache GETs. Violating them (e.g. `GET /orders/delete`) causes real incidents.

**Flow of status codes we use:** `201 Created` + `Location` header for creation; `200` for an idempotent *replay*; `204` for empty success; `401` unauthenticated; `403` authenticated but not allowed; `404`; `409` conflict (stock, bad transition, idempotency); `422` validation; `402` payment declined; `503` upstream down.

**In this project.** `create_order` sets `Location` and returns `Idempotent-Replay: true` with 200 on replays.

**Mistakes.** `200` with `{"success": false}` bodies; using POST for everything; verbs in URLs. **Trade-off:** pure REST vs action endpoints - `POST /orders/{id}/cancel` is acceptable when a state change isn't naturally a field update; we chose `PATCH status` because a state machine guards it.

**Test.** `test_domain_errors_map_to_status_codes`, `test_idempotent_http_replay_status_codes`.

---

## 1.4 Path, query, header and body parameters

**Concept.** FastAPI decides where a parameter comes from by its declaration:

| Declared as | Source | Example in project |
|---|---|---|
| name appears in path `{order_id}` | path | `get_order(order_id: str)` |
| simple type, not in path | query | `?status=paid&limit=10` |
| Pydantic model | JSON body | `body: OrderCreate` |
| `Header(...)` | header | `Idempotency-Key` |
| `Query/Path/Header(...)` extras | add validation | `Query(ge=1, le=100)` |

**Why.** One declaration yields parsing, type conversion, validation, documentation **and** editor autocompletion.

**Flow.** Request → FastAPI reads the function signature (once, at startup) → for each request extracts raw strings → validates/coerces via Pydantic → passes typed values, or returns `422` listing every bad field.

**Minimal example.**
```python
@router.get("/{sku}/stock")
async def stock(sku: Annotated[str, Path(min_length=2)],
                verbose: bool = False,                              # query
                x_trace: Annotated[str | None, Header()] = None):   # header X-Trace
```

**In this project.** `create_order` uses all four kinds; `pagination()` in [deps.py](../app/api/deps.py) bundles `limit/offset` query params into a reusable dependency.

**Mistakes.** Secrets in query strings (they land in logs; our `/auth/register` does it only for lab brevity and says so); unbounded `limit`; underscore vs hyphen in headers (`idempotency_key` ↔ `Idempotency-Key` is converted automatically).

**Test.** `test_path_query_header_and_body_parameters`.

---

## 1.5 Pydantic validation and serialization

**Concept.** Pydantic models are Python classes whose fields are *validated and coerced at runtime* from untrusted input, then *serialized* back to JSON.

**Why.** Type hints alone don't stop bad data at runtime. Validation at the boundary means the inside of your program can trust its types.

**Flow.** JSON bytes → `dict` → `Model.model_validate` (types, constraints, custom validators) → object → … → `response_model` filters/serializes the return value.

**Minimal example.**
```python
class OrderItemIn(BaseModel):
    sku: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True)]
    quantity: int = Field(ge=1, le=100)

class OrderCreate(BaseModel):
    customer_email: EmailStr
    items: list[OrderItemIn] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def no_dupes(cls, v): ...
```

**In this project.** [app/schemas/orders.py](../app/schemas/orders.py). The *same* models are reused as **MCP tool input/output schemas** (Chapter 4/5): one definition, three consumers (REST validation, OpenAPI docs, LLM tool schema). `from_attributes=True` lets `OrderOut.model_validate(orm_obj)` convert ORM rows.

**Mistakes.** Returning ORM objects directly (leaks columns, lazy-load errors); `float` for money (we use integer cents); trusting client-sent prices (we always read price from the DB).

**Trade-offs.** Pydantic v2 is fast (Rust core) but strict by default (`"1"`→int coercion is allowed in lax mode; use `strict=True` when you need exactness).

**Test.** `test_pydantic_validation_returns_422_with_clean_envelope`, `test_duplicate_skus_rejected_by_custom_validator`.

---

## 1.6 Response models, status codes and exception handling

**Concept.** `response_model` declares the public shape of a response; FastAPI filters the returned object to it. Exceptions are translated to HTTP by *handlers*.

**Why.** Separating "what I have internally" from "what I promise publicly" prevents accidental leaks and keeps the contract stable.

**Flow.** Function returns object → validated against `response_model` → serialized → status code applied. If an exception escapes: the most specific registered handler runs; otherwise Starlette's 500.

**In this project.** [app/core/errors.py](../app/core/errors.py) defines domain errors with `status_code` + stable `code`. [app/api/errors.py](../app/api/errors.py) registers handlers producing one envelope:
```json
{"error": {"code": "insufficient_stock", "message": "...", "details": {...}, "request_id": "..."}}
```
The catch-all handler logs the traceback server-side and returns a generic message: no internals leak (`test_unhandled_exception_hides_internals`).

**Mistakes.** Raising `HTTPException` deep in services (couples them to HTTP; the MCP tools can't reuse them). Echoing Pydantic's raw error `input` (can reflect secrets): we return only `loc` + `msg`.

**Trade-offs.** RFC 7807 `application/problem+json` is the standard envelope; ours is simpler and carries `request_id`. Adopt 7807 if you have external consumers who expect it.

**Test.** `test_domain_errors_map_to_status_codes`, `test_response_model_never_leaks_internal_fields`.

---

## 1.7 Middleware, CORS and background tasks

**Middleware** wraps every request/response (logging, request id, metrics). **CORS** is a *browser* rule: a page from origin A may call API on origin B only if B's response says so via `Access-Control-Allow-Origin`; the browser sends a **preflight** `OPTIONS` first for non-simple requests. **Background tasks** run after the response is sent.

**In this project.**
- [middleware.py](../app/api/middleware.py): pure-ASGI `RequestContextMiddleware` → request id (honours incoming `X-Request-ID`), timing, Prometheus metrics, JSON access log. *Pure ASGI*, not `BaseHTTPMiddleware`, to avoid its known issues with background tasks/contextvars.
- CORS configured from `CORS_ORIGINS`, **explicit origins** (never `*` with credentials).
- `notify_customer` runs via `BackgroundTasks` after `201` is sent.

**Flow of middleware order.** `add_middleware` wraps outward: last added runs first. We add CORS last so *error responses also get CORS headers*; otherwise browsers show a misleading "CORS error" instead of your 401.

**Mistakes.** CORS is *not* security for your API (curl ignores it); `allow_origins=["*"]` + cookies; heavy work in `BackgroundTasks` (it shares the process: crash = lost). **Trade-off:** use a queue (Celery/RQ/SQS/Arq) for durable work: retries, visibility, scaling workers separately.

**Test.** `test_middleware_adds_request_id_and_honours_incoming_one`, `test_cors_preflight`, `test_background_task_runs_after_response`.

---

## 1.8 Async/await and concurrency

**Concept.** `await` pauses *this* coroutine while I/O is pending and lets the event loop run others. It gives **concurrency** (many things in flight), not parallelism (CPU cores).

**Why.** The enrichment endpoint needs CRM + shipping data. Sequentially that costs `t_crm + t_ship`; concurrently `max(t_crm, t_ship)`.

**Flow.** `asyncio.gather(a(), b())` schedules both as tasks → loop interleaves them at each `await` → results return in order.

**Minimal example.**
```python
crm, ship = await asyncio.gather(crm.get_customer(e), shipping.quote(...), return_exceptions=True)
```

**In this project.** `GET /orders/{id}/enriched` ([routers.py](../app/api/v1/routers.py)) uses `return_exceptions=True` so one failing enrichment *degrades* the response (`"degraded": ["crm"]`) instead of failing it. All connectors share long-lived `httpx.AsyncClient`s.

**Mistakes.** One `AsyncSession` shared across concurrent tasks (not safe: one session per task); `def` vs `async def` confusion; forgetting `await` (returns a coroutine object); CPU-bound work in async code (offload to a process pool).

**Trade-offs.** `async def` + async drivers (asyncpg) for I/O-heavy services; plain `def` + threads is fine and simpler for mostly-CPU or sync-library code.

**Test.** `test_async_concurrency_enrichment_degrades_gracefully`; `test_repository_atomic_reserve_prevents_overselling` (real concurrent writers).

---

## 1.9 OpenAPI and API versioning

**Concept.** FastAPI generates an **OpenAPI 3** document from your signatures/models; `/docs` (Swagger UI) and `/redoc` render it. It is also the contract for client SDK generators and contract tests.

**Why.** Docs derived from code can't drift from code.

**Versioning.** Breaking changes (rename/remove/semantic change) need a new version; additive ones don't. We use **URL versions** (`/api/v1`, `/api/v2`): [v2/routers.py](../app/api/v2/routers.py) returns `total: {amount_cents, currency}` + `links` while v1 stays unchanged.

**In this project.** `title/version/description/openapi_tags`, per-route `responses={402:..., 409:...}`, `examples` on `OrderCreate`. Open <http://localhost:8000/docs>, click **Authorize**, log in with `staff@example.com / staff12345`.

**Trade-offs.** URL versioning (visible, cache/route friendly) vs header/media-type versioning (clean URLs, harder to test). Whatever you choose: publish a deprecation policy, add `Deprecation`/`Sunset` headers, and keep old versions running until usage hits zero.

**Test.** `test_openapi_and_versioning`, `test_v2_changes_shape_but_v1_unchanged`.

---

## Common mistakes cheat-sheet
1. Blocking calls inside `async def`. 2. Business logic inside routers. 3. Returning ORM objects. 4. `HTTPException` inside services. 5. CORS `*` with credentials. 6. Unbounded pagination. 7. Module-level resources instead of lifespan. 8. 200-with-error bodies. 9. No request id → impossible to correlate logs. 10. Trusting client-provided prices/ids.

## Interview questions (scenario-based)

**Q1. Your endpoint takes 300 ms normally but under load every endpoint becomes slow, though CPU is low. What do you check?**
*A.* Suspect a blocked event loop or exhausted pool. Look for a sync call inside `async def` (`requests`, `time.sleep`, sync SQLAlchemy, large JSON/CPU work). Confirm with `asyncio` debug mode / loop-lag metric / py-spy. Then check DB pool saturation (`pool_timeout` errors, waiting connections) and downstream timeouts. Fixes: async libraries, `def` endpoint (thread pool) for unavoidable sync code, `run_in_threadpool`/process pool for CPU, size pools to `workers × (pool_size+overflow) < DB max_connections`.

**Q2. A mobile client retries `POST /orders` on flaky networks and customers get double-charged. Design the fix.**
*A.* Make POST idempotent with a client-generated `Idempotency-Key`: store `(user, key) → request hash → order id` under a unique constraint in the **same transaction** as the order; on replay with same hash return the original (200 + `Idempotent-Replay`); same key + different body → 409; concurrent duplicates → unique violation → 409 "retry". Also pass the key to the payment provider. (See `OrderService.create_order`.)

**Q3. Frontend devs say "CORS error" on your 401 responses. Why, and how do you fix it?**
*A.* The error response lacks CORS headers because CORS middleware sits *inside* the failing layer, or the exception bypasses it. Add CORSMiddleware as the **outermost** middleware (added last) and ensure exception handlers return normal responses. The real bug is the 401; the browser just hides it behind CORS.

**Q4. Where does validation belong: Pydantic model, service, or database?**
*A.* All three, each for a different reason: Pydantic for shape/format at the boundary (cheap, gives 422); service for business rules that need state (stock, ownership, transitions); database constraints (`CHECK stock >= 0`, `UNIQUE`) as the last line of defence against races and bugs.

**Q5. You must change `total_cents` into a `{amount, currency}` object. 3 mobile versions are in the wild. What now?**
*A.* Breaking change → new version (`/api/v2`) while v1 keeps serving the old shape; share the service layer so both versions have identical rules and only the DTO mapping differs; announce deprecation with `Sunset` header; monitor v1 traffic per client version; remove only when negligible. Alternative: additive field `total` alongside `total_cents` (no break) and deprecate later.

**Q6. When would you choose `BackgroundTasks` over Celery?**
*A.* `BackgroundTasks` for best-effort, short, in-process work whose loss is tolerable (log shipping, cache warm). Celery/queue when the work must survive restarts, needs retries/backoff, rate limiting, scheduling or independent scaling (emails, invoices, webhooks). Mention outbox pattern for "DB write + message" atomicity.

**Q7. `/ready` vs `/health`?**
*A.* Liveness (`/health`): "should the orchestrator restart me?"; cheap, no dependencies. Readiness (`/ready`): "should traffic be routed to me?"; checks DB. A DB blip should make the pod *unready* (drained), not *dead* (restart storm).

**Q8. Why return 404 instead of 403 for another customer's order?**
*A.* 403 confirms the resource exists (information leak / enumeration). Returning 404 for "not yours" treats unauthorized and nonexistent identically. (`OrderService.get_order`.)

**Q9. Two independent downstream calls make your endpoint slow. How do you speed it up without breaking error behaviour?**
*A.* `asyncio.gather` with `return_exceptions=True`, then decide per dependency whether failure is fatal or degradable; add per-call timeouts; keep a circuit breaker so a dead dependency doesn't add its timeout to every request.

**Q10. How do you keep OpenAPI docs accurate for error responses?**
*A.* Declare `responses={402: ..., 409: ...}` (optionally with a shared error model) on routes, keep domain errors with stable `code`s, and add contract tests (schemathesis/openapi diff in CI) so spec changes are reviewed as API changes.
