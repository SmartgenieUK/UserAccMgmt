from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import EmailStr
from app.schemas.common import APIModel


class AdminUserRead(APIModel):
    id: UUID
    email: EmailStr
    is_active: bool
    is_verified: bool
    created_at: datetime


class AdminDisableRequest(APIModel):
    disable: bool
