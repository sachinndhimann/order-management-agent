"""Passwords, JWTs, roles and scopes (Chapter 3: auth, RBAC, OAuth2).

Design: ONE token format is used by FastAPI *and* the MCP server (shared HS256
secret in this lab; in production use asymmetric keys + JWKS so the MCP server
only holds the public key). Roles map to OAuth2-style scopes; MCP tools
check scopes, REST endpoints check roles.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from enum import StrEnum

import jwt

from app.core.config import Settings
from app.core.errors import AuthenticationError


class Role(StrEnum):
    ADMIN = "admin"
    STAFF = "staff"
    CUSTOMER = "customer"


# RBAC table: role -> scopes. Least privilege: customers can read, not mutate.
ROLE_SCOPES: dict[Role, list[str]] = {
    Role.ADMIN: ["orders:read", "orders:write", "inventory:read", "reports:read"],
    Role.STAFF: ["orders:read", "orders:write", "inventory:read"],
    Role.CUSTOMER: ["orders:read", "inventory:read"],
}


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """scrypt (memory-hard, stdlib) -> 'salt$hash' base64. Never store plaintext."""
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    salt_b64, _ = stored.split("$", 1)
    candidate = hash_password(password, salt=base64.b64decode(salt_b64))
    return hmac.compare_digest(candidate, stored)  # constant-time compare


@dataclass(frozen=True)
class Principal:
    """Who is calling. Built from a verified token, passed down to services."""

    user_id: int
    email: str
    role: Role

    @property
    def scopes(self) -> list[str]:
        return ROLE_SCOPES[self.role]


def create_access_token(p: Principal, settings: Settings, *, expires_in: int | None = None) -> str:
    now = int(time.time())
    claims = {
        "sub": str(p.user_id),
        "email": p.email,
        "role": p.role.value,
        "scope": " ".join(p.scopes),  # OAuth2 convention: space-delimited string
        "iat": now,
        "exp": now + (expires_in if expires_in is not None else settings.access_token_minutes * 60),
        "iss": "order-agent",
        "aud": "order-agent",
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pin the algorithm: blocks alg=none attacks
            audience="order-agent",
            issuer="order-agent",
        )
        return Principal(int(claims["sub"]), claims["email"], Role(claims["role"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid or expired token") from exc
