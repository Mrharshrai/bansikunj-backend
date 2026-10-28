"""Payment persistence.

Lookups are deliberately narrow: a payment is always found either by the order
it belongs to, or by the provider order id that a webhook/verification quotes.
The latter is locked with ``FOR UPDATE`` before it is changed, so two deliveries
of the same webhook cannot both observe ``created`` and both write ``paid``.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.payment import Payment


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, payment: Payment) -> Payment:
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def get_by_order_id(
        self, order_id: uuid.UUID | str, *, for_update: bool = False
    ) -> Payment | None:
        statement = select(Payment).where(
            Payment.order_id == self._as_uuid(order_id)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_provider_order_id(
        self, provider_order_id: str, *, for_update: bool = False
    ) -> Payment | None:
        statement = select(Payment).where(
            Payment.provider_order_id == provider_order_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def save(self, payment: Payment) -> Payment:
        await self.session.flush()
        return payment

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


__all__ = ["PaymentRepository"]
