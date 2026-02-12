from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthError
from app.models import RefreshToken
from app.security.hashing import hash_token, verify_token
from app.security.jwt import create_access_token
from app.utils.security import generate_token_secret, split_token
from app.utils.time import utcnow


class TokenService:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def create_access_token(self, user_id: str, email: str, role: str, org_id: str, scopes: list[str]):
        token, expires_in = create_access_token(
            settings=self.settings,
            subject=str(user_id),
            email=email,
            role=role,
            org_id=org_id,
            scopes=scopes,
        )
        return token, expires_in

    async def create_refresh_token(self, user_id: str, ip: str | None, user_agent: str | None) -> str:
        token_id = uuid.uuid4()
        secret = generate_token_secret(32)
        token_hash = hash_token(secret)
        expires_at = utcnow() + timedelta(days=self.settings.REFRESH_TOKEN_EXPIRE_DAYS)
        refresh = RefreshToken(
            id=token_id,
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            ip_address=ip,
            user_agent=user_agent,
        )
        self.session.add(refresh)
        await self.session.flush()
        return f"{token_id}.{secret}"

    async def verify_refresh_token(self, token: str) -> RefreshToken:
        token_id_str, secret = split_token(token)
        try:
            token_id = uuid.UUID(token_id_str)
        except ValueError:
            raise AuthError("Invalid refresh token", code="refresh_invalid")
        result = await self.session.execute(select(RefreshToken).where(RefreshToken.id == token_id))
        refresh = result.scalar_one_or_none()
        if not refresh:
            raise AuthError("Invalid refresh token", code="refresh_invalid")
        if refresh.revoked_at is not None or refresh.expires_at <= utcnow():
            raise AuthError("Refresh token expired or revoked", code="refresh_expired")
        if not verify_token(secret, refresh.token_hash):
            raise AuthError("Invalid refresh token", code="refresh_invalid")
        return refresh

    async def rotate_refresh_token(self, token: str, ip: str | None, user_agent: str | None) -> str:
        refresh = await self.verify_refresh_token(token)
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == refresh.id)
            .values(revoked_at=utcnow(), last_used_at=utcnow())
        )
        return await self.create_refresh_token(refresh.user_id, ip, user_agent)

    async def revoke_refresh_token(self, token: str) -> None:
        refresh = await self.verify_refresh_token(token)
        await self.session.execute(
            update(RefreshToken).where(RefreshToken.id == refresh.id).values(revoked_at=utcnow())
        )

    async def revoke_all_tokens_for_user(self, user_id: str) -> None:
        await self.session.execute(
            update(RefreshToken).where(RefreshToken.user_id == user_id).values(revoked_at=utcnow())
        )
