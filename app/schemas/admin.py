from __future__ import annotations

from pydantic import EmailStr
from app.schemas.common import APIModel


class AdminUserRead(APIModel):
    id: str
    email: EmailStr
    is_active: bool
    is_verified: bool
    created_at: str


class AdminDisableRequest(APIModel):
    disable: bool
