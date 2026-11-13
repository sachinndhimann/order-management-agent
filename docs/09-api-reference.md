# API Reference

Interactive docs while the stack is running: <http://localhost:8000/docs> (Swagger UI) · `/redoc` · `/openapi.json`.

## Demo users (seeded)
| Email | Password | Role |
|---|---|---|
| admin@example.com | admin12345 | admin |
| staff@example.com | staff12345 | staff |
| ada@example.com | customer12345 | customer |

## REST (v1, prefix `/api/v1`)
| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/auth/register?email=&password=` | public | creates a *customer* |
| POST | `/auth/token` (form: `username`,`password`) | public | returns JWT (30 min) |
| GET | `/auth/me` | any | |
| GET | `/products?limit&offset&in_stock` | any | paginated |
| GET | `/products/{sku}/stock` | any | |
| POST | `/orders` | any* | header `Idempotency-Key`; `201` / `200` replay; `402`,`409`,`422`. *customers: own email only |
| GET | `/orders?status&customer_email&sort&limit&offset` | any* | *customers see only their own |
| GET | `/orders/{id}` | any* | `404` if not visible |
| PATCH | `/orders/{id}/status` `{status}` | staff/admin (customer: cancel only) | state machine |
| GET | `/orders/{id}/enriched` | any* | concurrent CRM + shipping lookups; `degraded[]` |
| GET | `/reports/sales?days=` | admin | PostgreSQL reporting connector |
| GET | `/reports/low-stock?threshold=` | admin | |
| POST | `/agent/chat` `{message}` | any | LLM + MCP tools using caller's token |

**v2:** `GET /api/v2/orders/{id}` (structured money + links).
**Ops:** `GET /health` (liveness), `GET /ready` (DB), `GET /metrics` (Prometheus).

### Order state machine
```
pending ─▶ paid ─▶ shipped ─▶ delivered
   │         │
   └─▶ cancelled ◀┘      (cancel from paid: refund + restock)
```

### Error envelope
```json
{"error": {"code": "insufficient_stock", "message": "Not enough stock for HD-400",
           "details": {"sku": "HD-400"}, "request_id": "9f1c..."}}
```
| HTTP | code | meaning |
|---|---|---|
| 401 | `unauthenticated` | missing/invalid/expired token |
| 403 | `forbidden` | role not allowed |
| 404 | `not_found` | unknown or not visible |
| 409 | `conflict`, `insufficient_stock`, `invalid_status_transition` | state conflicts / idempotency |
| 402 | `payment_failed` | card declined |
| 422 | `request_validation_failed`, `validation_failed` | bad input / rule |
| 503 | `upstream_unavailable` | connector failed (timeout, retries exhausted, breaker open) |
| 500 | `internal_error` | bug (details only in server logs) |

## MCP: orders server (`http://localhost:8001/mcp`, Bearer = same JWT)
| Tool | Scope | Hints | Description |
|---|---|---|---|
| `get_order(order_id)` | orders:read | read-only | one order |
| `list_orders(status?, customer_email?, limit, offset, sort)` | orders:read | read-only | paginated |
| `check_inventory(sku)` | inventory:read | read-only | stock level |
| `create_order(customer_email, items[], idempotency_key?)` | orders:write | write | reserve → charge → paid |
| `update_order_status(order_id, status)` | orders:write | destructive, idempotent | state machine; cancel refunds |
| `sales_summary(days)` | reports:read | read-only | admin only |

Resources: `inventory://catalog`, `orders://{order_id}` · Prompt: `triage_order(order_id)`.

Role → scopes: admin = all; staff = orders:read/write, inventory:read; customer = orders:read, inventory:read.

## MCP: external logistics server (`http://localhost:8002/mcp`, Bearer = `EXTERNAL_MCP_API_KEY`)
`track_shipment(tracking_number)` (pattern `^[A-Z]{2}\d{6,12}$`; numbers ending in `0` → not found), `estimate_delivery(postal_code)`.

## Simulated partners (`http://localhost:9000`)
| Service | Endpoints | Failure injection |
|---|---|---|
| Payment (OAuth2) | `POST /payment/oauth/token`, `/payment/v1/charges`, `/payment/v1/refunds` | amount `1300` → 402 decline; `1399` → 503 once then OK; tokens last 60 s |
| CRM (`X-CRM-Key`) | `PUT/GET /crm/contacts` | email containing `down` → 503 |
| Shipping (`X-API-Key`) | `GET /shipping/rates?zip&kg` | |

Seed data: `KB-100` ($89.99, 25), `MS-200` ($29.99, 40), `MN-300` ($249.99, 8), `HD-400` ($39.99, **2**), `CB-500` ($13.00, 100: triggers the **decline**), `RT-600` ($13.99, 50: triggers a **503-then-retry** at the payment vendor).
