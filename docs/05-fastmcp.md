# Chapter 5: FastMCP

> **Builds on:** [Chapter 4](04-mcp.md) (protocol concepts). **Next:** [Chapter 6: Connectors](06-connectors.md).
> **Code:** [app/mcp_server/server.py](../app/mcp_server/server.py), [Dockerfile](../Dockerfile), [docker-compose.yml](../docker-compose.yml) · **Tests:** [tests/test_04_05_mcp_fastmcp.py](../tests/test_04_05_mcp_fastmcp.py)

FastMCP is the Python framework that turns functions into MCP servers (and ships a client): decorators for tools/resources/prompts, schema generation from type hints, auth providers, middleware, dependency injection and test helpers. This repo uses the standalone `fastmcp` package (v4.x). The v0 prototype used the older `mcp.server.fastmcp` that is bundled in the official SDK; the decorators look alike, the standalone package is more feature-rich.

---

## 5.1 Installing and configuring FastMCP

```bash
uv pip install fastmcp           # or: pip install fastmcp
python -c "import fastmcp; print(fastmcp.__version__)"
```
Pin the version in `pyproject.toml` (`fastmcp>=2.12` here; the lab was verified on 4.1.8). **Why pin:** MCP and FastMCP are young; APIs and defaults move (we hit two behaviour differences while building, see 5.7/5.9).

**Configuration** is by constructor arguments, not magic files:
```python
FastMCP("order-management", instructions="...", auth=verifier, middleware=[...],
        mask_error_details=True, lifespan=lifespan)
```
**In this project.** `create_server()` in [server.py](../app/mcp_server/server.py). Settings (`JWT_SECRET`, `DATABASE_URL`…) come from the same typed `Settings` as the REST API.

---

## 5.2 Creating MCP servers

**Concept.** A `FastMCP` object is a registry of tools/resources/prompts plus transports.

**Minimal working server (copy-paste).**
```python
from fastmcp import FastMCP
mcp = FastMCP("demo")

@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

if __name__ == "__main__":
    mcp.run()                                   # stdio
    # mcp.run(transport="http", port=8001)      # Streamable HTTP at /mcp
```
**Flow.** `FastMCP(...)` → decorators register `FunctionTool` objects (schema generated now) → `run()` starts a transport loop → each `tools/call` runs middleware → auth check → argument validation → dependency resolution → your function → result serialization.

**In this project.** `create_server()` is a **factory** (not a module-level global) so tests can build servers with different auth/config. Entry point `main()` selects transport with `--transport`.

**Mistakes.** Module-level side effects (DB connections at import); one server object shared across tests.

---

## 5.3 Registering tools using decorators

**Concept.** `@mcp.tool` registers a function. Name = function name, description = docstring, schema = type hints (+ `Field`). Options: `annotations` (hints to clients), `tags`, `auth`, `output_schema`, `timeout`.

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
          auth=require_scopes("orders:read"), tags={"orders"})
async def get_order(order_id: Annotated[str, Field(min_length=36, max_length=36)],
                    svc: OrderService = Depends(get_order_service),
                    who: Principal = Depends(get_principal)) -> OrderOut:
    """Retrieve one order (items, status, total in cents, payment reference) by its id."""
    return OrderOut.model_validate(await svc.get_order(order_id, who))
```

**Why annotations.** `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` let hosts decide auto-approve vs "ask the user first" (we mark `update_order_status` destructive because cancelling refunds).

**Tool design rules we followed.** Few, precisely named tools; verbs for actions; every parameter described; sensible bounds (`limit ≤ 50`); money in integer cents stated in the description; destructive tools idempotent where possible; **no business logic in the tool** (delegates to services).

**Dependency injection in tools.** `Depends(...)` params are resolved per call and hidden from the schema. For `yield`-style providers fastmcp needs an **async context manager** (`@asynccontextmanager`); a bare async generator is *not* entered - the injected value would be the generator object. Write `param: T = Depends(fn)` (default-value form).

**Test.** `test_tool_discovery_lists_tools_with_json_schema`, `test_full_order_lifecycle_through_tools`.

---

## 5.4 Creating resources and prompts

```python
@mcp.resource("inventory://catalog", mime_type="application/json")
async def catalog() -> list[dict]: ...

@mcp.resource("orders://{order_id}")          # URI template → "resource template"
async def order_resource(order_id: str) -> dict: ...

@mcp.prompt
def triage_order(order_id: str) -> str:
    return f"Investigate order {order_id}. Fetch it with get_order, ..."
```
**Why.** Resources give hosts read-only context without a model decision; prompts give users one-click workflows.

**Flow.** `resources/read orders://abc` → template matched → `order_id="abc"` → function → content block. FastMCP's `resource_security` rejects path-traversal-looking parameters by default.

**Mistakes.** Putting writes in resources; unauthenticated resource templates that bypass service authorization (ours reads via a service with a system principal for the demo - in production resolve the caller's identity like tools do).

**Test.** `test_resources_and_prompts_are_discoverable`, `test_resource_template_rejects_path_traversal`.

---

## 5.5 Typed input/output schemas

Type hints are the schema. Reuse Pydantic models for rich shapes (`OrderCreate`, `Page[OrderOut]`); use `Annotated[..., Field(...)]` for constraints and descriptions; return a model to publish `outputSchema` and `structuredContent`.

**Check what the LLM sees** (this is the single most useful debugging habit):
```python
async with Client(mcp) as c:
    for t in await c.list_tools():
        print(t.name, json.dumps(t.inputSchema, indent=2))
```
**Mistakes.** `-> dict` (no output schema, no validation); `Any`; defaults that hide required choices; huge enums.

**Test.** `test_call_tool_returns_structured_and_text_content` (`res.data.available` is re-hydrated into a typed object client-side).

---

## 5.6 Context and logging

`ctx: Context = CurrentContext()` gives a tool access to per-request MCP facilities: `await ctx.info(...)` (log notification to the client), `report_progress`, `read_resource`, session/request ids, `elicit` (ask the user), and client sampling.

```python
@mcp.tool
async def create_order(..., ctx: Context = CurrentContext()):
    await ctx.info(f"creating order for {customer_email}")
```
**Two kinds of logs, don't confuse them.** *Server logs* (stderr/JSON/OTel; for operators) vs *MCP log notifications* (sent to the **client**; for the host/user). Never put secrets or PII in client-visible notifications. Note: protocol-level logging notifications are being deprecated in newer spec revisions (the test run warns); keep operator logging as the source of truth.

**Test.** `test_context_logging_reaches_client`.

---

## 5.7 Error handling

**Layers.** (1) Argument validation errors (automatic, before your code). (2) Domain errors (`AppError`). (3) Unexpected exceptions.

**Policy in this repo.** `mask_error_details=True` hides internals of unexpected errors. A `DomainErrorMiddleware` (`on_call_tool`) converts `AppError` into `ToolError("[insufficient_stock] Not enough stock for HD-400")`, which the **model can read and act on**. Implementation detail worth remembering: with masking on, FastMCP wraps the original exception in a generic `ToolError` *before* middleware sees it; the middleware recovers the original from `__cause__`.

**Why `isError` matters.** Errors travel as *results* (`isError:true`), not protocol failures, so the LLM sees them in context and self-corrects. Protocol errors (unknown method) are different.

**Mistakes.** Returning `"error: ..."` strings with `isError=false` (the model can't tell success from failure); exposing stack traces; swallowing errors and returning empty results.

**Test.** `test_domain_errors_become_tool_errors_with_codes`, `test_unexpected_exception_is_masked`.

---

## 5.8 stdio and HTTP transport

```bash
python -m app.mcp_server.server --transport stdio          # for local hosts (Claude Desktop, IDEs)
python -m app.mcp_server.server --transport http --port 8001
```
Claude Desktop-style config for stdio:
```json
{"mcpServers": {"orders": {"command": "python", "args": ["-m", "app.mcp_server.server", "--transport", "stdio"],
                           "env": {"DATABASE_URL": "sqlite+aiosqlite:///./dev.db"}}}}
```
HTTP mode wraps an ASGI app (`mcp.http_app(path="/mcp")`), so you can also **mount it into an existing FastAPI app** or put it behind your gateway. We run it as a separate process/container so it scales and is secured independently of the REST API.

**Stdio mode skips JWT auth (trusted local process) and uses an admin principal.** Don't ship that to a shared host.

**Stateless vs stateful HTTP.** Stateful sessions need sticky routing or a shared store behind load balancers; `stateless_http=True` creates per-request transports (simpler to scale, no server-initiated streams).

---

## 5.9 Testing MCP tools

Two levels, both in [tests/test_04_05_mcp_fastmcp.py](../tests/test_04_05_mcp_fastmcp.py):

1. **In-memory** (fast, default): `async with Client(server) as c: await c.call_tool(...)` - no sockets. Auth is bypassed (build the server with `auth_enabled=False`).
2. **Real HTTP**: serve `server.http_app()` with uvicorn on an ephemeral port (`http_url` fixture in [conftest.py](../tests/conftest.py)) to test JWT auth, scopes and sessions.

Rules of thumb: assert on `structured_content`; test error paths with `raise_on_error=False`; test discovery (names, schemas, annotations) so accidental contract changes fail CI; use fake ports (`FakePayment`) so no vendor is called; add an **LLM-free agent test** with `ScriptedLLM` (Chapter 7).

**Lifespan gotcha we hit.** The in-memory transport runs the server `lifespan` on *every client connection*. We had a lifespan that disposed the DB engine; the second connection found an empty database. Fix: closing pools is opt-in (`close_pools_on_shutdown=True`) and enabled only in `main()`.

---

## 5.10 Deploying MCP servers using Docker

**Files.** [Dockerfile](../Dockerfile) (one image for all roles) and [docker-compose.yml](../docker-compose.yml).

```dockerfile
FROM python:3.12-slim
COPY pyproject.toml ./ ; RUN uv pip install --system -r pyproject.toml   # cached dependency layer
COPY app ./app ...
USER appuser                                                               # never root
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```
```yaml
orders-mcp:
  <<: *app
  command: python -m app.mcp_server.server --transport http --port 8001
  depends_on: {migrate: {condition: service_completed_successfully}}
```
**Production checklist.** Bind `0.0.0.0` inside the container only; terminate TLS at the proxy; secrets via `*_FILE`/orchestrator secrets (not baked into images); healthchecks; resource limits; one readiness probe that checks the DB; don't expose the MCP port publicly without auth (here ports are published for the lab only); pin base image digests; run `pip-audit`/Trivy in CI.

---

## Interview questions (scenario-based)

**Q1. Your tool takes `db: Session` from a global. How do you restructure it for testability and correct transactions?**
*A.* Resolve a per-call session via `Depends(get_session)` implemented as an `@asynccontextmanager` that commits/rolls back; build the service from it; tools stay thin. Tests inject a different container.

**Q2. The LLM sends `quantity: "two"`. What happens?**
*A.* The generated JSON Schema/Pydantic validation rejects it before the function runs and returns a tool error describing the violation; the model can retry. No business code executes, no partial side effects.

**Q3. How do you hide admin tools from non-admins?**
*A.* `auth=require_scopes("reports:read")` on the tool (or tag-based `restrict_tag`) with scopes from the verified token. Unauthorized tools disappear from `tools/list` and calls fail. Also enforce in the service in case a tool is registered incorrectly (defence in depth).

**Q4. A tool occasionally hangs for minutes and the agent stalls. What controls exist?**
*A.* Per-tool `timeout=`, client-side timeouts and retries with breaker (`McpServerConnector`), timeouts on the underlying HTTP/DB calls (statement timeout), and an agent step/time budget. Report timeouts to the model as tool errors.

**Q5. Local development uses stdio; production uses HTTP. What changes in your code?**
*A.* Only the transport line and auth. Business logic is transport-agnostic. Must remember: no stdout prints in stdio; identity comes from the token over HTTP but from the OS user over stdio.

**Q6. How do you version tools without breaking existing agents?**
*A.* Treat tool schemas like API contracts: additive optional params are safe; renames/removals are breaking → add `create_order_v2` (or FastMCP tool `version=`) and deprecate in the description; keep a discovery test that snapshots schemas.

**Q7. `mask_error_details=True` hides useful errors from the LLM. Is that a bug?**
*A.* Only unexpected exceptions are masked. Expected, actionable failures must be raised as `ToolError` with safe text (we translate domain errors in middleware). That gives the model what it needs without leaking internals.

**Q8. Why register tools inside `create_server()` instead of at module level?**
*A.* Factories avoid import-time side effects, allow per-test configuration (auth on/off, different settings), and enable running several isolated server instances in one process.

**Q9. How would you add rate limiting for a chatty agent?**
*A.* FastMCP's rate-limiting middleware (token bucket per client id/tool), plus per-user quotas at the host, plus idempotency so retries are harmless.

**Q10. Walk me through containerising this for production.**
*A.* Multi-stage slim image, non-root user, dependencies layer cached, one image per role with different commands, healthchecks, migrations as a one-shot job (not at app start), secrets from the platform, TLS at ingress, JWKS-based verification instead of a shared HS256 secret, structured logs to stdout, resource limits and HPA on request latency/CPU.
