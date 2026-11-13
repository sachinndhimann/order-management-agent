"""Interactive CLI: talk to the agent from a terminal.

    python -m app.agent.cli --email staff@example.com --password staff12345 "How many MN-300 are in stock?"
"""
from __future__ import annotations

import argparse
import asyncio

from app.agent.service import AgentService
from app.container import build_container
from app.core.config import get_settings
from app.core.security import Principal, Role, create_access_token


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("message")
    ap.add_argument("--role", default="staff", choices=[r.value for r in Role])
    args = ap.parse_args()
    settings = get_settings()
    # Dev convenience: mint a token directly. In real use the CLI would log in via /auth/token.
    token = create_access_token(Principal(1, f"{args.role}@example.com", Role(args.role)), settings)
    c = build_container(settings)
    try:
        r = await AgentService(c).chat(args.message, token)
    finally:
        await c.aclose()
    for t in r.trace:
        print(f"  tool> {t.tool}({t.arguments}) -> {t.result[:120]}")
    print(r.answer)


if __name__ == "__main__":
    asyncio.run(main())
