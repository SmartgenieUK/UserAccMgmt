from __future__ import annotations

from pydantic import Field
from app.schemas.common import APIModel


class TokenPair(APIModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Access token lifetime in seconds")


class TokenPayload(APIModel):
    sub: str
    email: str
    role: str
    org_id: str
    scopes: list[str]
    iat: int
    exp: int
