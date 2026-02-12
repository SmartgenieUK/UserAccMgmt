from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models import User
from app.schemas.admin import AdminUserRead, AdminDisableRequest
from app.schemas.common import MessageResponse
from app.security.dependencies import require_scopes

router = APIRouter()


@router.get("/admin/users", response_model=list[AdminUserRead])
async def list_users(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_scopes(["admin:users:read"])),
):
    result = await session.execute(select(User))
    return list(result.scalars().all())


@router.patch("/admin/users/{user_id}/disable", response_model=MessageResponse)
async def disable_user(
    user_id: str,
    data: AdminDisableRequest,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_scopes(["admin:users:write"])),
):
    user = await session.get(User, user_id)
    if user:
        user.is_active = not data.disable
        await session.commit()
    return MessageResponse(message="User updated")
