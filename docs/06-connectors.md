# Chapter 6: Custom Connectors

> **Builds on:** Chapters 2 (ports/DI) and 3 (services). **Next:** [Chapter 7: Integration](07-integration.md).
> **Code:** [app/connectors/](../app/connectors/), [mock_services/app.py](../mock_services/app.py) · **Tests:** [tests/test_06_connectors.py](../tests/test_06_connectors.py)

| Connector | Kind | Auth | File |
|---|---|---|---|
| Shipping rates | generic **REST API** | API key | [shipping.py](../app/connectors/shipping.py) |
| Logistics | **external MCP server** | bearer (API key) | [mcp_external.py](../app/connectors/mcp_external.py) |
| Reporting | **PostgreSQL** (read-only) | DB role | [reporting.py](../app/connectors/reporting.py) |
| CRM | **simulated CRM** (PascalCase schema) | API key header | [crm.py](../app/connectors/crm.py) |
| Payment | **external payment API** | **OAuth2 client-credentials** | [payment.py](../app/connectors/payment.py) |
| LLM | HTTP API | bearer | [agent/llm.py](../app/agent/llm.py) |

Shared toolkit: [base.py](../app/connectors/base.py).

---

## 6.1 Adapter pattern and connector architecture

**Concept.** The **Adapter** pattern converts one interface into another a client expects. Here: the domain defines a **port** (`PaymentPort.charge(order_id, amount_cents, ...)`); a **connector/adapter** implements it by speaking the vendor's HTTP dialect.

**Why.** Vendors differ (auth, JSON shapes, error semantics, quirks) and change. Without an adapter every vendor detail leaks into services and tests, and switching vendors means rewriting business code.

**Flow (payment).**
```
OrderService ──charge(order_id, cents)──▶ PaymentPort
                                            ▲ implemented by
                                   PaymentConnector (adapter)
                      ┌─ auth: OAuth2ClientCredentials (token cache/refresh)
                      ├─ resilience: timeout, retry+backoff, circuit breaker
                      ├─ idempotency: Idempotency-Key header
                      └─ translation: vendor JSON/HTTP errors ⇄ PaymentResult / domain errors
```
**Layering inside a connector.** `BaseHttpConnector.request()` (transport + resilience) ← specific connector (endpoints + schema mapping) ← port (domain).

**Mistakes.** Letting vendor exceptions/JSON shapes escape the connector; one god `ApiClient` for all vendors; putting business rules in connectors.

**Trade-offs.** Hand-written adapters (ours; full control, tiny deps) vs generated SDK clients from OpenAPI (fast, but you still wrap them in an adapter) vs an integration platform (less code, less control).

---

## 6.2 OAuth2, API keys and bearer tokens

| Scheme | How | Good for | Risks |
|---|---|---|---|
| **API key** (`ApiKeyAuth`) | static secret in a header | simple server-to-server | no expiry/scoping; rotate by redeploy; leaks are long-lived |
| **Static bearer** (`BearerAuth`) | pre-issued token | PATs, internal | same as above |
| **OAuth2 client-credentials** (`OAuth2ClientCredentials`) | exchange `client_id+secret` for a short-lived access token | machine-to-machine at scale | must cache + refresh correctly |

**Strategy pattern.** `AuthStrategy` has two methods: `headers()` (give me credentials for this request) and `invalidate()` (the server rejected them). Connectors take any strategy, so changing auth never touches endpoint code.

---

## 6.3 Token refresh and expiry

**Concept.** Access tokens are short-lived (mock vendor: 60 s). Handle expiry **proactively** (refresh before it dies) and **reactively** (a 401 means the server disagrees: refresh once).

**Flow** (`OAuth2ClientCredentials`).
1. `headers()` under an `asyncio.Lock`: if no token or `now ≥ expires_at − skew (30 s)` → `POST /oauth/token` → cache `access_token` and `expires_in`.
2. Request sent. If `401` → `invalidate()` clears the cache → **one** free retry with a fresh token (`auth_retried` flag prevents loops) → still 401 ⇒ `ConnectorAuthError`.
3. The lock makes 10 concurrent requests share **one** refresh (no stampede on the auth server).

**Mistakes.** Fetching a token per request; no skew (token expires in flight); refreshing in a loop on persistent 401 (revoked credentials); logging tokens; using wall clock for expiry math (we use a monotonic clock, injectable for tests).

**Tests.** `test_oauth2_token_is_cached_and_refreshed_before_expiry`, `test_401_triggers_one_token_refresh_and_retry`, `test_persistent_401_raises_auth_error_without_looping`, `test_concurrent_callers_share_a_single_token_fetch`, `test_payment_refund_and_expired_token_recovery`.

---

## 6.4 HTTPX asynchronous clients

**Concept.** `httpx.AsyncClient` is the async HTTP client: connection pooling, HTTP/1.1 keep-alive (HTTP/2 optional), granular timeouts, transports.

**In this project.** One long-lived client per connector (`self._client` in `BaseHttpConnector`), created at startup by the container and closed on shutdown; `base_url` set once; `httpx.Timeout(timeout, connect=min(timeout, 2))`.

**Transport injection.** Connectors accept `transport=`. Tests pass `httpx.ASGITransport(app=mock_app)` to run the *real connector against the real simulated vendor app* with **no sockets**; unit tests use `respx` to script exact responses.

**Mistakes.** New client per call; no timeout (httpx default is 5 s but explicit is better; requests library has *none*); forgetting `aclose()`; reading huge bodies into memory (stream them).

---

## 6.5 Timeouts, retries and exponential backoff

**Timeouts.** Every outbound call needs a deadline: connect (fail fast, 2 s), read (vendor-dependent). Without them one slow vendor consumes all your workers.

**Retries - decision table** (`BaseHttpConnector.request`):

| Outcome | Action | Why |
|---|---|---|
| timeout / connection error | retry if safe | transient |
| `429, 502, 503, 504` | retry if safe | transient/overload |
| other `5xx` | fail (no blind retry) | might be a deterministic bug |
| `401` | refresh token, retry once | credentials, not outage |
| other `4xx` | **never retry**, don't trip breaker | our request is wrong |
| 2xx | success | |

**"If safe".** Retry only when repeating cannot do harm: idempotent methods (GET/PUT/DELETE) or a POST the caller marked `idempotent=True` because it carries an `Idempotency-Key` (payment charge). `test_post_is_not_retried_unless_marked_idempotent`.

**Exponential backoff with full jitter.** `delay(n) = uniform(0, min(max, base·2^(n−1)))`. Doubling gives the vendor time to recover; **jitter** de-synchronises clients so they don't retry in lockstep (thundering herd). `jitter=False` + injectable `sleep` makes tests instant and exact (`sleeps == [0.2, 0.4]`).

**Mistakes.** Retrying non-idempotent calls; retrying without backoff; unbounded attempts; retries at *every* layer (3×3×3 = 27 calls: retry at one layer); ignoring `Retry-After` (add it for 429 in production).

---

## 6.6 Circuit breakers

**Concept.** A breaker watches failures and, when a dependency looks down, *fails fast* instead of waiting for timeouts.

```
CLOSED ──(N consecutive failures)──▶ OPEN ──(reset_seconds)──▶ HALF_OPEN
  ▲                                    │ reject immediately        │ one probe call
  └────────────── success ─────────────┴──────── failure ──────────┘ → OPEN
```
**Why.** During an outage, each request would otherwise burn `timeout × retries` of a worker, exhausting capacity and turning a partner outage into *your* outage. Failing fast also reduces load on the struggling vendor so it can recover.

**In this project.** Per-connector `CircuitBreaker`; `CircuitOpenError` is mapped to `UpstreamUnavailableError` (503). Clients/agents see a clear "unavailable" instead of hanging. Decline (`402`) and 4xx do **not** count as failures: the vendor is healthy.

**Mistakes.** One shared breaker for unrelated endpoints (one bad route trips all); thresholds too low (flapping) or too high (useless); no metric/alert on state; forgetting half-open probe limits under high concurrency (our simple version lets probes through until one resolves; libraries like `pybreaker`/`aiobreaker` add stricter control).

**Tests.** `test_circuit_breaker_opens_fails_fast_then_recovers`, `test_half_open_probe_failure_reopens`, `test_external_mcp_connector_retries_then_trips_breaker`.

---

## 6.7 Schema transformation

**Concept.** Translate vendor representations ⇄ domain DTOs at the boundary.

| Vendor says | We say | Where |
|---|---|---|
| `{"id":"ch_1","amount":1999,"status":"succeeded"}` | `PaymentResult(reference, status, amount_cents)` | `PaymentConnector.charge` |
| `{"ContactId":7,"FirstName":"Ada","EmailAddress":"ADA@X.COM","LoyaltyTier":"GOLD"}` | `CustomerProfile(crm_id="7", email="ada@x.com", tier="gold")` | `to_profile` (pure function) |
| `{"price":"12.50"}` | `cost_cents=1250` | `Decimal`, never `float` |
| HTTP 402 `card_declined` | `PaymentFailedError` (business outcome) | payment connector |
| HTTP 503 / timeout / breaker open | `UpstreamUnavailableError` | all connectors |

**Why.** One place to adapt when the vendor changes; domain stays in clean snake_case/cents; tests of mapping are pure-function tests.

**Mistakes.** Using vendor field names across the codebase; `float` money (`4.10*100 = 409.99999…`); trusting vendor fields blindly (validate with Pydantic in production).

**Tests.** `test_crm_schema_transformation_pure_function`, `test_shipping_picks_cheapest_and_converts_money_exactly`, `test_payment_happy_path_transforms_vendor_json`.

---

## 6.8 Secret management

**Rules.** (1) Secrets never in git (`.env` is ignored; `.env.example` documents names). (2) Typed settings with `SecretStr` so `repr()`/logs print `**********`. (3) Support **file-based secrets** (`FOO_FILE=/run/secrets/foo`) because env vars leak via `docker inspect`, crash dumps and child processes ([config.py](../app/core/config.py)). (4) Least privilege per secret (the reporting DB role is read-only). (5) Rotate: short-lived tokens (OAuth) > long-lived keys; design for two valid keys during rotation. (6) Production: a manager (AWS Secrets Manager, Vault, Doppler, K8s secrets + KMS), not files in the repo.

**Found in the v0 prototype.** `.env` with a live API key was untracked but not gitignored - one `git add .` from a leak. If a key ever was committed: **rotate it**; deleting the commit isn't enough.

**Test.** `test_secrets_are_masked_and_loadable_from_files`.

---

## 6.9 Connector testing and mocking

Pyramid used here:
1. **Pure function tests** (mapping): fastest.
2. **Scripted HTTP** with `respx`: exact status sequences (503, 503, 200), timeouts, 401 flows.
3. **Simulated vendor over ASGI** ([mock_services/app.py](../mock_services/app.py)): real connector ↔ real HTTP semantics (OAuth tokens, idempotency, declines) without sockets. The same mock runs in Docker Compose so humans can poke it.
4. **Fake ports** in business tests (`FakePayment`): services tested without any HTTP.
5. (Production add-on) **Contract tests** against the vendor sandbox in a nightly job, and record/replay (VCR) for regression.

**Failure injection built into the mock** so you can *see* resilience work: amount `1300` → decline; `1399` → 503 once then success; 60 s tokens; CRM email containing `down` → 503.

**Mistakes.** Only happy-path tests; real network in unit tests (flaky, slow); mocks that don't behave like the vendor (keep them honest with contract tests).

---

## 6.10 The connectors in detail

### REST API (shipping)
`ShippingConnector.quote()` → `GET /rates?zip&kg` with API key → picks the cheapest rate, converts to cents. Used by the `/enriched` endpoint concurrently with the CRM call.

### External MCP server
`McpServerConnector` wraps `fastmcp.Client` with timeout/retry/breaker and normalises results to `McpCallResult(text, data, is_error)`. See Chapter 4.7.

### PostgreSQL (reporting)
`ReportingConnector`: separate engine and credentials (`REPORTING_DATABASE_URL`, read-only role created by [docker/initdb/01-reporting-role.sql](../docker/initdb/01-reporting-role.sql)), small pool, `SET LOCAL statement_timeout`, and a **closed menu** of parameterised queries (`QUERIES`). Unknown report names raise `KeyError`: there is deliberately **no "run SQL" tool**. Exposed to admins via `GET /reports/sales` and the MCP tool `sales_summary`.

### Simulated CRM
`PUT /contacts` (idempotent upsert → safe to auto-retry), `GET /contacts?email=` (404 → `None`). PascalCase ⇄ snake_case mapping. **Best-effort**: `OrderService._sync_crm` swallows `UpstreamUnavailableError` so a CRM outage never fails a paid order.

### <a name="payment"></a>External payment API
`POST /v1/charges` with `Idempotency-Key: charge-<order_id>`, OAuth2 client-credentials, retries on 5xx, `402` → `PaymentFailedError`, `POST /v1/refunds` for cancellations. **Known gap (documented, not hidden):** charge and DB commit are not atomic; see Chapter 3.4 for outbox/reconciliation.

---

## Interview questions (scenario-based)

**Q1. The payment API times out. Did the customer get charged? What do you do?**
*A.* Unknown - the response was lost. That's why every charge carries an idempotency key derived from the order id: retrying returns the original charge instead of a new one. If retries are exhausted, leave the order `pending`, don't release stock yet, and let a reconciliation job query the provider by key/metadata to resolve it. Never auto-retry without the key.

**Q2. Vendor has a 20-minute outage. Your API p99 goes from 200 ms to 15 s. Why and how to prevent?**
*A.* Every request waits `timeout × attempts`. Add a circuit breaker so requests fail fast (503) after N failures, lower timeouts, bulkhead the connector (limited concurrency/pool), and degrade non-critical dependencies (CRM). Alert on breaker state.

**Q3. You see 5,000 simultaneous token requests to the vendor's auth server each time tokens expire. Cause/fix?**
*A.* Token stampede: many tasks notice expiry together. Single-flight refresh under a lock, refresh early (skew), jitter, and share the token cache across workers if possible (Redis) or accept one refresh per process.

**Q4. After deployment, all calls to the CRM return 401 but nothing changed in code. What are the suspects?**
*A.* Rotated/revoked key, wrong secret mounted (`*_FILE` vs env precedence), clock skew for signed tokens, trailing newline in a secret file (we `strip()`), different header name. Check connector metrics (`outcome=http_401`), secret source, and vendor audit logs. Never log the key; log its fingerprint.

**Q5. When is retrying a POST safe?**
*A.* When the operation is idempotent by design: the server dedupes by an idempotency key, or the endpoint is naturally idempotent (PUT-like upsert). Otherwise a retry after an ambiguous failure can duplicate side effects.

**Q6. Why not have the LLM write SQL against Postgres?**
*A.* Injection/exfiltration/DoS risk, unpredictable load, bypasses authorization. Expose narrow parameterised tools (or a read-only role, row-level security, timeouts and a vetted query menu) and put the semantic layer in code you review.

**Q7. How do you test that backoff works without waiting seconds?**
*A.* Inject `sleep` (record durations), disable jitter or seed it, inject a fake clock for breaker/token expiry, and script responses with `respx`. Tests assert on the recorded delays `[0.2, 0.4]` and call counts.

**Q8. The vendor returns 500 with a body `{"error":"duplicate"}` for a retried request. How should the connector treat it?**
*A.* It depends on documented semantics. If the vendor signals "already processed" it is success-equivalent; map it in the adapter (parse the code) and fetch/return the original result. Blind 5xx retry/failure rules are defaults, vendor knowledge overrides them.

**Q9. Add a second payment provider (fallback). Where does it plug in?**
*A.* Implement `PaymentPort` again (e.g. `AdyenConnector`) and compose a `FailoverPayment(primary, secondary)` adapter in the container. Services don't change. Beware double-charging: only fail over on *definite* failures (breaker open / connection refused), not ambiguous timeouts, unless both honour the same idempotency reference.

**Q10. Which of your connector failures should surface to the user as errors vs be hidden?**
*A.* Business-critical (payment) → surface (402/503) and don't lose the order intent; optional enrichments (CRM, shipping ETA) → degrade silently with logs/metrics and a `degraded` marker; auth/config errors → page the on-call (they will not self-heal).
