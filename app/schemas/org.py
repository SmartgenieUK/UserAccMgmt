from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import EmailStr
from app.schemas.common import APIModel


class OrganizationCreate(APIModel):
    name: str
    slug: str | None = None


class OrganizationRead(APIModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class MembershipRead(APIModel):
    org_id: str
    role: str


class InviteRequest(APIModel):
    email: EmailStr
    role: str


class InviteResponse(APIModel):
    message: str


class InvitationAcceptRequest(APIModel):
    token: str
