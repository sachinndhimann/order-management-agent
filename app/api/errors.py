"""Exception handlers (Chapter 1): turn exceptions into consistent JSON.

Every error response has the same envelope, so clients write ONE error parser:
    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import AppError
from app.core.observability import request_id_var

log = logging.getLogger("api")


def _body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {},
                      "request_id": request_id_var.get()}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(_body(exc.code, exc.message, exc.details), exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Pydantic's raw errors can echo user input; keep only location + message.
        details = [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(_body("request_validation_failed", "Request validation failed", {"errors": details}), 422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        # Log the real error server-side; tell the client nothing internal.
        log.exception("unhandled_error")
        return JSONResponse(_body("internal_error", "Internal server error"), 500)
