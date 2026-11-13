"""Repository layer (Chapter 3): the ONLY code that talks SQL.

WHY: services express business rules ("can't ship a cancelled order"); they
should not know whether data lives in Postgres, SQLite or a test fake.
Repositories are small, take an AsyncSession in the constructor (injected), and
never commit - the unit of work (session_scope / the Depends) owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKey, Order, Product, User

SORTABLE = {"created_at": Order.created_at, "total_cents": Order.total_cents, "status": Order.status}


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        return (await self.session.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()

    async def get(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user


class ProductRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_sku(self, sku: str) -> Product | None:
        return (await self.session.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()

    async def list(self, *, limit: int, offset: int, in_stock_only: bool = False) -> tuple[list[Product], int]:
        q = select(Product)
        if in_stock_only:
            q = q.where(Product.stock > 0)
        total = (await self.session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        rows = (await self.session.execute(q.order_by(Product.sku).limit(limit).offset(offset))).scalars().all()
        return list(rows), total

    async def try_reserve(self, product_id: int, qty: int) -> bool:
        """Atomic conditional decrement - the race-free way to reserve stock.

        BAD:  read stock, check in Python, write stock-qty  (two buyers both see 1 left)
        GOOD: one UPDATE ... WHERE stock >= qty; rowcount tells us if we won.
        """
        res = await self.session.execute(
            update(Product).where(Product.id == product_id, Product.stock >= qty)
            .values(stock=Product.stock - qty))
        return res.rowcount == 1

    async def release(self, product_id: int, qty: int) -> None:
        await self.session.execute(update(Product).where(Product.id == product_id)
                                   .values(stock=Product.stock + qty))

    async def add(self, product: Product) -> Product:
        self.session.add(product)
        await self.session.flush()
        return product


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, order: Order) -> Order:
        self.session.add(order)
        await self.session.flush()  # flush = send SQL inside the txn, without committing
        return order

    async def get(self, order_id: str) -> Order | None:
        return await self.session.get(Order, order_id)

    async def list(self, *, status: str | None, customer_email: str | None, sort: str,
                   limit: int, offset: int) -> tuple[list[Order], int]:
        q = select(Order)
        if status:
            q = q.where(Order.status == status)
        if customer_email:
            q = q.where(Order.customer_email == customer_email.lower())
        total = (await self.session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        # Sort column comes from a WHITELIST dict, never from raw user text => no SQL injection.
        col = SORTABLE[sort.lstrip("-")]
        q = q.order_by(col.desc() if sort.startswith("-") else col.asc(), Order.id)  # id = stable tiebreak
        rows = (await self.session.execute(q.limit(limit).offset(offset))).scalars().all()
        return list(rows), total


class IdempotencyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def find(self, key: str, user_id: int) -> IdempotencyKey | None:
        return (await self.session.execute(
            select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.user_id == user_id)
        )).scalar_one_or_none()

    async def add(self, rec: IdempotencyKey) -> None:
        self.session.add(rec)
        await self.session.flush()  # unique constraint fires here on a concurrent duplicate
