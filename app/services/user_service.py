from __future__ import annotations

import re
import unicodedata

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.models import User
from app.utils.profile_schema import ProfileSchemaRegistry

COMPANY_MAX = 120
LINKEDIN_MAX = 255
# The whole LinkedIn rule: https, host exactly linkedin.com or www.linkedin.com (ASCII case folding only), a /in/ or
# /company/ path with one plain slug, no userinfo, port, query, fragment or escapes. No network check.
_LINKEDIN = re.compile(r"(?i:https://(?:www\.|[a-z]{2}\.)?linkedin\.com)/(?:in|company)/[A-Za-z0-9_-]+/?", re.ASCII)


def _trimmed(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text.", code=f"{field}_invalid")
    return value.strip() or None


def validate_signup_profile(body) -> tuple[str | None, str | None]:
    """Both optional fields from a PUT body: trimmed, empty means cleared. Raises a closed 422 code; writes nothing."""
    if not isinstance(body, dict):
        raise ValidationError("The request body must be a JSON object.", code="profile_body_invalid")
    company = _trimmed(body.get("company_name"), "company_name")
    if company is not None:
        if len(company) > COMPANY_MAX:
            raise ValidationError("The company name is too long.", code="company_name_too_long")
        if any(unicodedata.category(ch) == "Cc" for ch in company):
            raise ValidationError("The company name has a character that is not allowed.", code="company_name_invalid")
    url = _trimmed(body.get("linkedin_url"), "linkedin_url")
    if url is not None:
        # A link copied from a browser often carries ?tracking or #anchor; keep only the page address.
        url = url.split("?", 1)[0].split("#", 1)[0]
        if len(url) > LINKEDIN_MAX:
            raise ValidationError("The LinkedIn address is too long.", code="linkedin_url_too_long")
        if not _LINKEDIN.fullmatch(url):
            raise ValidationError("The LinkedIn address must be an https linkedin.com /in/ or /company/ page.", code="linkedin_url_invalid")
    return company, url


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

    async def set_signup_profile(self, user: User, body) -> User:
        user.company_name, user.linkedin_url = validate_signup_profile(body)
        return user

    async def deactivate_user(self, user: User) -> None:
        user.is_active = False
