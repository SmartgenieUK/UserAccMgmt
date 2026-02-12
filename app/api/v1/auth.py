from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_hooks, rate_limit_dependency
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.auth import (
    RegisterRequest,
    LoginRequest,
    RefreshRequest,
    LogoutRequest,
    PasswordResetRequest,
    PasswordResetConfirm,
    ChangePasswordRequest,
    ChangeEmailRequest,
    ChangeEmailConfirm,
    VerifyEmailRequest,
)
from app.schemas.common import MessageResponse
from app.schemas.token import TokenPair
from app.security.dependencies import get_current_user
from app.security.csrf import validate_csrf_token
from app.services.auth_service import AuthService
from app.services.token_service import TokenService
from app.services.email_service import EmailService
from app.services.audit_service import AuditService

router = APIRouter()


@router.post("/register", response_model=MessageResponse, status_code=201)
async def register(
    data: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
    _=Depends(rate_limit_dependency(limit=5, period_seconds=3600, key_prefix="register")),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.register(
        email=data.email,
        password=data.password,
        display_name=data.display_name,
        org_name=data.org_name,
    )
    return MessageResponse(message="Registration successful. Please verify your email.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    data: VerifyEmailRequest,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.verify_email(data.token)
    return MessageResponse(message="Email verified")


@router.post("/login", response_model=TokenPair)
async def login(
    data: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
    _=Depends(rate_limit_dependency(limit=10, period_seconds=60, key_prefix="login")),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    access, refresh, expires_in = await service.login(
        email=data.email,
        password=data.password,
        org_id=data.org_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    data: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    if settings.USE_COOKIE_AUTH:
        csrf_header = request.headers.get("X-CSRF-Token")
        csrf_cookie = request.cookies.get(settings.COOKIE_NAME_CSRF)
        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            raise ValueError("CSRF token mismatch")
        validate_csrf_token(settings, csrf_cookie)
        refresh_token = request.cookies.get(settings.COOKIE_NAME_REFRESH)
    else:
        refresh_token = data.refresh_token

    if not refresh_token:
        raise ValueError("Refresh token required")

    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    access, new_refresh, expires_in = await service.refresh(
        refresh_token,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return TokenPair(access_token=access, refresh_token=new_refresh, expires_in=expires_in)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    data: LogoutRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    refresh_token = data.refresh_token or request.cookies.get(settings.COOKIE_NAME_REFRESH)
    if not refresh_token:
        raise ValueError("Refresh token required")
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.logout(refresh_token)
    return MessageResponse(message="Logged out")


@router.post("/password-reset/request", response_model=MessageResponse)
async def password_reset_request(
    data: PasswordResetRequest,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
    _=Depends(rate_limit_dependency(limit=5, period_seconds=3600, key_prefix="pwreset")),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.request_password_reset(data.email)
    return MessageResponse(message="If the email exists, a reset link was sent")


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def password_reset_confirm(
    data: PasswordResetConfirm,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.confirm_password_reset(data.token, data.new_password)
    return MessageResponse(message="Password updated")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    data: ChangePasswordRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.change_password(current_user, data.current_password, data.new_password)
    return MessageResponse(message="Password changed")


@router.post("/change-email/request", response_model=MessageResponse)
async def change_email_request(
    data: ChangeEmailRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.request_email_change(current_user, data.new_email, data.current_password)
    return MessageResponse(message="Email change verification sent")


@router.post("/change-email/confirm", response_model=MessageResponse)
async def change_email_confirm(
    data: ChangeEmailConfirm,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    hooks=Depends(get_hooks),
):
    service = AuthService(
        session=session,
        settings=settings,
        hooks=hooks,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
    )
    await service.confirm_email_change(data.token)
    return MessageResponse(message="Email updated")
