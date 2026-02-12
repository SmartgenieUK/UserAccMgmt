from __future__ import annotations

from typing import Any
from pydantic import EmailStr
from app.schemas.common import APIModel


class UserRead(APIModel):
    id: str
    email: EmailStr
    display_name: str | None
    avatar_url: str | None
    locale: str | None
    timezone: str | None
    is_active: bool
    is_verified: bool
    custom_fields: dict[str, Any]
    custom_schema_version: int
    created_at: str
    updated_at: str | None


class UserUpdate(APIModel):
    display_name: str | None = None
    avatar_url: str | None = None
    locale: str | None = None
    timezone: str | None = None
    custom_fields: dict[str, Any] | None = None
    custom_schema_version: int | None = None
