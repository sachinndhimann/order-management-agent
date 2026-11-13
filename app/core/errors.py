"""Domain errors (Chapter 1 & 3: exception handling, separation of concerns).

WHY: services must not know about HTTP, and MCP tools must not know about
FastAPI. Services raise *domain* errors; each transport maps them:
  - FastAPI: exception handlers (app/api/errors.py) -> RFC 7807-ish JSON + status
  - MCP:     ToolError with the same message (app/mcp_server/server.py)
Each error carries a stable machine-readable `code` so clients (and the LLM)
can branch on it instead of parsing English.
"""
from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code, code = 404, "not_found"


class ConflictError(AppError):
    status_code, code = 409, "conflict"


class ValidationFailed(AppError):
    status_code, code = 422, "validation_failed"


class InsufficientStockError(ConflictError):
    code = "insufficient_stock"


class InvalidTransitionError(ConflictError):
    code = "invalid_status_transition"


class PaymentFailedError(AppError):
    status_code, code = 402, "payment_failed"


class AuthenticationError(AppError):
    status_code, code = 401, "unauthenticated"


class PermissionDeniedError(AppError):
    status_code, code = 403, "forbidden"


class UpstreamUnavailableError(AppError):
    """A connector gave up (timeout / retries exhausted / breaker open)."""

    status_code, code = 503, "upstream_unavailable"
