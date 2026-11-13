"""Idempotent demo data: 3 users (one per role) and a small catalogue."""
from __future__ import annotations

import asyncio

from app.container import build_container
from app.core.config import Settings
from app.core.security import Role, hash_password
from app.db.models import Product, User
from app.db.session import session_scope
from app.repositories.repos import ProductRepository, UserRepository

USERS = [("admin@example.com", "admin12345", Role.ADMIN), ("staff@example.com", "staff12345", Role.STAFF),
         ("ada@example.com", "customer12345", Role.CUSTOMER)]
PRODUCTS = [("KB-100", "Mechanical Keyboard", 8999, 25), ("MS-200", "Wireless Mouse", 2999, 40),
            ("MN-300", "27-inch Monitor", 24999, 8), ("HD-400", "USB-C Hub", 3999, 2),
            ("CB-500", "Braided Cable", 1300, 100),  # 1300 cents => mock payment DECLINES (demo)
            ("RT-600", "Retry Demo Widget", 1399, 50)]  # 1399 cents => mock payment 503s once, retry succeeds


async def seed(maker) -> None:
    async with session_scope(maker) as s:
        users, products = UserRepository(s), ProductRepository(s)
        for email, pw, role in USERS:
            if not await users.get_by_email(email):
                await users.add(User(email=email, password_hash=hash_password(pw), role=role.value))
        for sku, name, price, stock in PRODUCTS:
            if not await products.get_by_sku(sku):
                await products.add(Product(sku=sku, name=name, price_cents=price, stock=stock))


async def main(settings: Settings | None = None) -> None:
    c = build_container(settings)
    await seed(c.sessionmaker)
    await c.aclose()
    print("seeded")


if __name__ == "__main__":
    asyncio.run(main())
