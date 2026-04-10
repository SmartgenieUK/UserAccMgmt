from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import Field
from app.schemas.common import APIModel


class ApplicationCreate(APIModel):
    name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[str] = []
    allowed_scopes: list[str] = []


class ApplicationRead(APIModel):
    id: UUID
    org_id: UUID
    name: str
    client_id: str
    redirect_uris: list[str]
    allowed_scopes: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ApplicationCreated(ApplicationRead):
    client_secret: str  # Only returned once at creation time


class ApplicationUpdate(APIModel):
    name: str | None = None
    redirect_uris: list[str] | None = None
    allowed_scopes: list[str] | None = None
    is_active: bool | None = None


class RotateSecretResponse(APIModel):
    client_id: str
    client_secret: str  # New secret, only shown once


class ClientCredentialsRequest(APIModel):
    client_id: str
    client_secret: str
    scopes: list[str] | None = None
