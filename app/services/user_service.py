from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import User
from app.utils.profile_schema import ProfileSchemaRegistry


class UserService:
    def __init__(self, session: AsyncSession, settings: Settings, profile_registry: ProfileSchemaRegistry):
        self.session = session
        self.settings = settings
        self.profile_registry = profile_registry

    async def update_profile(self, user: User, data: dict) -> User:
        if "custom_fields" in data and data["custom_fields"] is not None:
            version = data.get("custom_schema_version") or user.custom_schema_version
            validated = self.profile_registry.validate(version, data["custom_fields"])
            user.custom_fields = validated
            user.custom_schema_version = version

        for field in ["display_name", "avatar_url", "locale", "timezone"]:
            if field in data and data[field] is not None:
                setattr(user, field, data[field])

        return user

    async def deactivate_user(self, user: User) -> None:
        user.is_active = False
