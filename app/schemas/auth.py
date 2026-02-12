from __future__ import annotations

from pydantic import EmailStr, Field
from app.schemas.common import APIModel


class RegisterRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str | None = None
    org_name: str | None = None


class LoginRequest(APIModel):
    email: EmailStr
    password: str
    org_id: str | None = None


class RefreshRequest(APIModel):
    refresh_token: str | None = None


class LogoutRequest(APIModel):
    refresh_token: str | None = None


class PasswordResetRequest(APIModel):
    email: EmailStr


class PasswordResetConfirm(APIModel):
    token: str
    new_password: str


class ChangePasswordRequest(APIModel):
    current_password: str
    new_password: str


class ChangeEmailRequest(APIModel):
    new_email: EmailStr
    current_password: str


class ChangeEmailConfirm(APIModel):
    token: str


class VerifyEmailRequest(APIModel):
    token: str


class OAuthAuthorizeResponse(APIModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(APIModel):
    code: str
    state: str
    redirect_uri: str | None = None
