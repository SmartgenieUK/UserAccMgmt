from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_profile_registry
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.user import UserRead, UserUpdate
from app.schemas.common import MessageResponse
from app.security.dependencies import get_current_user
from app.services.user_service import UserService

router = APIRouter()


@router.get("/me", response_model=UserRead)
async def get_me(current_user=Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserRead)
async def update_me(
    data: UserUpdate,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_profile_registry),
):
    service = UserService(session, settings, registry)
    await service.update_profile(current_user, data.model_dump(exclude_unset=True))
    await session.commit()
    return current_user


@router.delete("/me", response_model=MessageResponse)
async def delete_me(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_profile_registry),
):
    service = UserService(session, settings, registry)
    await service.deactivate_user(current_user)
    await session.commit()
    return MessageResponse(message="Account deactivated")
