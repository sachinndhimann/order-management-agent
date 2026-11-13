# Interview Preparation Guide

Each chapter ends with 10 scenario-based questions and model answers (70 in total). This page gives you the study plan, the cross-cutting system-design questions that combine chapters, a rapid-fire sheet, and how to talk about this project.

| Chapter | Questions |
|---|---|
| [1 FastAPI](01-fastapi.md#interview-questions-scenario-based) | async blocking, idempotent POST, CORS, validation layers, versioning, background tasks, probes, 404 vs 403, concurrency, OpenAPI |
| [2 Dependency Injection](02-dependency-injection.md#interview-questions-scenario-based) | service locator, commit timing, shared sessions, overrides, Protocols, multi-tenancy, settings, teardown, singletons, shared wiring |
| [3 Architecture](03-backend-architecture.md#interview-questions-scenario-based) | overselling, commit-after-charge, keyset paging, zero-downtime migrations, pool exhaustion, IDOR, idempotent PATCH, outbox, DTOs, tracing |
| [4 MCP](04-mcp.md#interview-questions-scenario-based) | MCP vs function-calling, tool selection, prompt injection, data authorization, transports, degraded servers, double charge, content vs structured, stdio pitfalls, end-to-end flow |
| [5 FastMCP](05-fastmcp.md#interview-questions-scenario-based) | DI in tools, validation, hiding admin tools, timeouts, stdio→HTTP, versioning, error masking, factories, rate limits, containerising |
| [6 Connectors](06-connectors.md#interview-questions-scenario-based) | charge timeout, outage latency, token stampede, 401s, retrying POST, text-to-SQL, testing backoff, vendor quirks, failover, error surfacing |
| [7 Integration](07-integration.md#interview-questions-scenario-based) | hallucinated ids, bulk cancel, name collisions, LLM outage, testing LLMs, stale reads, streaming, memory, injection, direct DB access |

## 4-week study plan
1. **Week 1: FastAPI + DI.** Read chapters 1-2, run `pytest tests/test_01*.py tests/test_02*.py -v`, then *break things on purpose*: remove `scope="function"`, remove `await`, add `time.sleep` in an endpoint, observe.
2. **Week 2: Architecture.** Chapter 3. Implement keyset pagination as an exercise; add a `version` column for optimistic locking; write an Alembic migration with expand/contract.
3. **Week 3: MCP + FastMCP.** Chapters 4-5. Add a new tool (`list_low_stock`), a prompt, run the server over stdio and attach it to an MCP-capable desktop client.
4. **Week 4: Connectors + integration.** Chapters 6-7. Add a second payment adapter + failover; write an eval suite of 20 prompts.

## Cross-cutting system-design questions

**S1. Design the order-creation path so that no order is lost, none is duplicated, and no customer is double-charged.**
Client idempotency key → unique constraint → reserve stock atomically → create `pending` order **and commit** → authorize/charge with provider idempotency key → on success mark `paid` (or capture) → on ambiguous failure leave `pending` + reconciliation job → on definite failure compensate (release stock). Outbox for downstream events (email, CRM). Metrics for `pending` age. Discuss trade-off between our simpler "single transaction" version and the safer "pending-then-finalize".

**S2. Scale the system 100×.** Stateless API/MCP replicas; PgBouncer; read replicas for reporting connector; cache catalogue; queue for notifications; rate limit + per-user budgets for the agent; keyset pagination; partition `orders` by month; async workers for long agent tasks; autoscale on latency/queue depth.

**S3. Secure an agent that can move money.** Least-privilege per-user tokens; scopes per tool; server-side rules authoritative; human confirmation for destructive hints; allow-listed MCP servers; audit log of every tool call with the acting user; idempotency; spend limits (max order total without approval); anomaly alerts; red-team prompt-injection tests.

**S4. How would you migrate from `HS256` shared secret to production auth?** Introduce an IdP (OIDC), verify with JWKS on both FastAPI and FastMCP (`JWTVerifier(jwks_uri=...)`), map IdP groups to roles/scopes, dual-accept old tokens during a rollout window, rotate and delete the shared secret.

**S5. The business wants "refund within 30 days" via the agent. Where do you implement the rule?** In `OrderService` (state machine + time rule + role), expose through the existing `update_order_status`/new `refund_order` tool; tool descriptions mention constraints but the service enforces them; add tests at the service level and a scripted-agent test.

## Rapid-fire cheat sheet
- **ASGI vs WSGI:** async + websockets/streaming vs sync-only.
- **`def` vs `async def` endpoint:** thread pool vs event loop; never block the loop.
- **Idempotent methods:** GET, PUT, DELETE (+ POST with key). PATCH: depends.
- **401 vs 403 vs 404:** unauthenticated / forbidden / hide existence.
- **422 vs 400 vs 409:** validation / malformed / state conflict.
- **Depends cache:** per request. **yield dependency:** teardown reverse order, `scope="function"` to run before response.
- **Session per request**, commit in unit of work, repositories only `flush`.
- **Pool math:** `workers × (pool_size + overflow) ≤ max_connections`.
- **Offset vs keyset:** simple/jumpable vs fast/stable.
- **Migrations:** review autogenerate; expand/contract; run as a job once.
- **MCP primitives:** tools (model), resources (app), prompts (user).
- **MCP transports:** stdio (local, no auth) / Streamable HTTP (remote, auth).
- **MCP lifecycle:** initialize → initialized → list → call → shutdown.
- **`isError`:** tool failure as data. Protocol errors are different.
- **Retry only if safe;** backoff + jitter; breaker; timeouts everywhere.
- **Money:** integer cents / `Decimal`, never `float`.
- **Secrets:** never in git, `SecretStr`, file-based, rotate.

## How to present this project (90-second pitch)
"I built an AI order-management agent. A FastAPI service and a FastMCP server share one service/repository layer over PostgreSQL, so humans and LLMs go through identical business rules. The agent discovers tools over MCP from both our server and a third-party logistics server, forwards the user's own JWT so RBAC is enforced server-side, and handles failures: idempotent writes, atomic stock reservation, OAuth2-authenticated payment connector with retries, backoff and a circuit breaker, graceful degradation when the CRM or an MCP server is down. Everything is tested without network: 90+ tests with scripted LLM, fake vendors and real DB, and it runs with one `docker compose up`."

**Be ready to discuss honestly:** the commit-after-charge gap, HS256 shared secret in the lab, password grant being a lab simplification, offset pagination limits, per-chat MCP handshake cost, in-process breaker state (per replica, not global).
