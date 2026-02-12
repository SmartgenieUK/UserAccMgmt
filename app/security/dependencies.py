from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings, Settings
from app.db.session import get_session
from app.models import User, Membership, Organization, Role
from app.schemas.token import TokenPayload
from app.security.jwt import decode_access_token
from app.security.permissions import resolve_scopes
from app.utils.context import org_id_ctx

bearer = HTTPBearer(auto_error=False)


async def get_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> TokenPayload:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_access_token(settings, credentials.credentials)
        return TokenPayload(**payload)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def get_current_user(
    payload: TokenPayload = Depends(get_token_payload),
    session: AsyncSession = Depends(get_session),
) -> User:
    result = await session.execute(select(User).where(User.id == payload.sub))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User inactive or not found")
    return user


async def get_current_membership(
    request: Request,
    payload: TokenPayload = Depends(get_token_payload),
    session: AsyncSession = Depends(get_session),
) -> Membership:
    org_id = request.headers.get("X-Org-Id") or payload.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization context required")
    org_id_ctx.set(org_id)
    result = await session.execute(
        select(Membership).where(Membership.user_id == payload.sub, Membership.org_id == org_id)
    )
    membership = result.scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=403, detail="No membership for organization")
    return membership


async def get_current_org(
    membership: Membership = Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
) -> Organization:
    result = await session.execute(select(Organization).where(Organization.id == membership.org_id))
    org = result.scalar_one_or_none()
    if not org or not org.is_active:
        raise HTTPException(status_code=403, detail="Organization inactive or not found")
    return org


def require_scopes(required: list[str]):
    async def _dependency(payload: TokenPayload = Depends(get_token_payload)) -> TokenPayload:
        for scope in required:
            if scope not in payload.scopes:
                raise HTTPException(status_code=403, detail="Insufficient permissions")
        return payload

    return _dependency


def resolve_token_scopes(payload: TokenPayload) -> list[str]:
    role = Role(payload.role)
    return resolve_scopes(role)
