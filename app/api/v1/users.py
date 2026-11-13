from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_profile_registry
from app.core.config import get_settings
from app.db.session import get_session
from app.core.exceptions import ValidationError
from app.schemas.user import SignupProfileRead, UserRead, UserUpdate
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


@router.put("/me/signup-profile", response_model=SignupProfileRead)
async def set_signup_profile(
    request: Request,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_profile_registry),
):
    """Set or clear the optional company name and LinkedIn URL of the signed-in user (the token's subject only)."""
    try:
        body = await request.json()
    except Exception:
        raise ValidationError("The request body must be a JSON object.", code="profile_body_invalid") from None
    await UserService(session, settings, registry).set_signup_profile(current_user, body)
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
