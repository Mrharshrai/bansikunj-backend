"""Refresh-token persistence, backing logout and refresh rotation."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.refresh_token import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        jti: str,
        expires_at: datetime,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        record = RefreshToken(
            user_id=user_id,
            jti=jti,
            expires_at=expires_at,
            user_agent=(user_agent or None) if user_agent else None,
            ip_address=ip_address,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_active_by_jti(self, jti: str) -> RefreshToken | None:
        """Return the token only if it exists and has not been revoked."""
        result = await self.session.execute(
            select(RefreshToken).where(
                RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def revoke_by_jti(self, jti: str, revoked_at: datetime) -> bool:
        result = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        return bool(result.rowcount)

    async def mark_replaced(self, jti: str, new_jti: str, revoked_at: datetime) -> None:
        """Retire a token that was exchanged for a new one."""
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(revoked_at=revoked_at, replaced_by_jti=new_jti)
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID, revoked_at: datetime) -> int:
        """Log the user out everywhere (used by logout-all and password change)."""
        result = await self.session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
            )
            .values(revoked_at=revoked_at)
        )
        return int(result.rowcount or 0)

    async def purge_expired(self, now: datetime) -> int:
        result = await self.session.execute(
            RefreshToken.__table__.delete().where(RefreshToken.expires_at < now)
        )
        return int(result.rowcount or 0)
