from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import ValidationError


class CustomProfileV1(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    department: str | None = None
    phone: str | None = None
    title: str | None = None


class ProfileSchemaRegistry:
    def __init__(self):
        self._schemas: dict[int, type[BaseModel]] = {}

    def register(self, version: int, schema: type[BaseModel]) -> None:
        self._schemas[version] = schema

    def validate(self, version: int, data: dict[str, Any]) -> dict[str, Any]:
        schema = self._schemas.get(version)
        if not schema:
            raise ValidationError("Unknown profile schema version", code="profile_schema_unknown")
        obj = schema(**data)
        return obj.model_dump()


def default_profile_registry() -> ProfileSchemaRegistry:
    registry = ProfileSchemaRegistry()
    registry.register(1, CustomProfileV1)
    return registry
