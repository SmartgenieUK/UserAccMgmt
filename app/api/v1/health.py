from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    await session.execute(text("SELECT 1"))
    redis = getattr(request.app.state, "redis", None)
    if redis:
        await redis.ping()
    return {"status": "ready"}


@router.get("/help")
async def api_help(settings=Depends(get_settings)):
    """Machine-readable API reference for calling applications."""
    base = settings.API_V1_PREFIX
    return {
        "name": settings.APP_NAME,
        "version": "1",
        "base_url": f"{settings.PUBLIC_BASE_URL}{base}",
        "authentication": {
            "type": "Bearer",
            "header": "Authorization",
            "description": "Pass access token as 'Authorization: Bearer <token>'. Obtain tokens via /login, /oauth, or /auth/token.",
        },
        "endpoints": {
            "public": [
                {
                    "method": "GET",
                    "path": f"{base}/health",
                    "description": "Health check",
                },
                {
                    "method": "GET",
                    "path": f"{base}/ready",
                    "description": "Readiness probe — checks database and Redis connectivity",
                },
                {
                    "method": "GET",
                    "path": f"{base}/help",
                    "description": "This API reference",
                },
                {
                    "method": "POST",
                    "path": f"{base}/register",
                    "description": "Register a new user account",
                    "body": {"email": "string", "password": "string", "display_name": "string|null", "org_name": "string|null"},
                    "rate_limit": "5/hour",
                },
                {
                    "method": "POST",
                    "path": f"{base}/login",
                    "description": "Authenticate and receive access + refresh tokens",
                    "body": {"email": "string", "password": "string", "org_id": "uuid|null"},
                    "response": {"access_token": "string", "refresh_token": "string", "expires_in": "int"},
                    "rate_limit": "10/minute",
                },
                {
                    "method": "POST",
                    "path": f"{base}/verify-email",
                    "description": "Verify email address with OTP code",
                    "body": {"email": "string", "otp": "string"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/resend-verification",
                    "description": "Resend email verification OTP",
                    "body": {"email": "string"},
                    "rate_limit": "3/hour",
                },
                {
                    "method": "POST",
                    "path": f"{base}/password-reset/request",
                    "description": "Request a password reset OTP via email",
                    "body": {"email": "string"},
                    "rate_limit": "5/hour",
                },
                {
                    "method": "POST",
                    "path": f"{base}/password-reset/confirm",
                    "description": "Confirm password reset with OTP",
                    "body": {"email": "string", "otp": "string", "new_password": "string"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/change-email/confirm",
                    "description": "Confirm email change with OTP",
                    "body": {"new_email": "string", "otp": "string"},
                },
                {
                    "method": "GET",
                    "path": f"{base}/oauth/{{provider}}/authorize",
                    "description": "Get OAuth authorization URL (provider: google, microsoft)",
                    "response": {"authorization_url": "string", "state": "string"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/oauth/{{provider}}/callback",
                    "description": "Exchange OAuth authorization code for tokens",
                    "body": {"code": "string", "state": "string", "redirect_uri": "string|null"},
                    "response": {"access_token": "string", "refresh_token": "string", "expires_in": "int"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/auth/token",
                    "description": "Client credentials grant — exchange client_id/secret for an access token",
                    "body": {"client_id": "string", "client_secret": "string", "scopes": "list[string]|null"},
                    "response": {"access_token": "string", "expires_in": "int", "scopes": "list[string]"},
                    "rate_limit": "30/minute",
                },
                {
                    "method": "POST",
                    "path": f"{base}/invitations/accept",
                    "description": "Accept an organization invitation (requires auth)",
                    "body": {"token": "string"},
                },
            ],
            "authenticated": [
                {
                    "method": "GET",
                    "path": f"{base}/me",
                    "description": "Get current user profile",
                },
                {
                    "method": "PATCH",
                    "path": f"{base}/me",
                    "description": "Update current user profile",
                    "body": {"display_name": "string|null"},
                },
                {
                    "method": "DELETE",
                    "path": f"{base}/me",
                    "description": "Deactivate current user account",
                },
                {
                    "method": "POST",
                    "path": f"{base}/refresh",
                    "description": "Refresh access token",
                    "body": {"refresh_token": "string"},
                    "response": {"access_token": "string", "refresh_token": "string", "expires_in": "int"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/logout",
                    "description": "Invalidate refresh token",
                    "body": {"refresh_token": "string"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/change-password",
                    "description": "Change password (authenticated user)",
                    "body": {"current_password": "string", "new_password": "string"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/change-email/request",
                    "description": "Request email change — sends OTP to new address",
                    "body": {"new_email": "string", "current_password": "string"},
                },
            ],
            "organizations": [
                {
                    "method": "POST",
                    "path": f"{base}/orgs",
                    "description": "Create organization",
                    "scopes": ["orgs:write"],
                    "body": {"name": "string"},
                },
                {
                    "method": "GET",
                    "path": f"{base}/orgs",
                    "description": "List organizations the user belongs to",
                    "scopes": ["orgs:read"],
                },
                {
                    "method": "POST",
                    "path": f"{base}/orgs/{{org_id}}/invite",
                    "description": "Invite a user to the organization (admin only)",
                    "scopes": ["invitations:write"],
                    "body": {"email": "string", "role": "admin|member|readonly"},
                },
            ],
            "applications": [
                {
                    "method": "POST",
                    "path": f"{base}/orgs/{{org_id}}/apps",
                    "description": "Register an OAuth application",
                    "scopes": ["apps:write"],
                    "body": {"name": "string", "allowed_scopes": "list[string]|null"},
                    "response": {"id": "uuid", "client_id": "string", "client_secret": "string (shown once)"},
                },
                {
                    "method": "GET",
                    "path": f"{base}/orgs/{{org_id}}/apps",
                    "description": "List applications for organization",
                    "scopes": ["apps:read"],
                },
                {
                    "method": "GET",
                    "path": f"{base}/orgs/{{org_id}}/apps/{{app_id}}",
                    "description": "Get application details",
                    "scopes": ["apps:read"],
                },
                {
                    "method": "PATCH",
                    "path": f"{base}/orgs/{{org_id}}/apps/{{app_id}}",
                    "description": "Update application",
                    "scopes": ["apps:write"],
                    "body": {"name": "string|null", "allowed_scopes": "list[string]|null", "is_active": "bool|null"},
                },
                {
                    "method": "POST",
                    "path": f"{base}/orgs/{{org_id}}/apps/{{app_id}}/rotate-secret",
                    "description": "Rotate client secret (invalidates previous secret)",
                    "scopes": ["apps:write"],
                },
                {
                    "method": "DELETE",
                    "path": f"{base}/orgs/{{org_id}}/apps/{{app_id}}",
                    "description": "Delete application",
                    "scopes": ["apps:write"],
                },
            ],
            "admin": [
                {
                    "method": "GET",
                    "path": f"{base}/admin/users",
                    "description": "List all users in the system",
                    "scopes": ["admin:users:read"],
                },
                {
                    "method": "PATCH",
                    "path": f"{base}/admin/users/{{user_id}}/disable",
                    "description": "Enable or disable a user account",
                    "scopes": ["admin:users:write"],
                    "body": {"disabled": "bool"},
                },
            ],
        },
        "scopes": {
            "profile:read": "Read own profile",
            "profile:write": "Update own profile",
            "orgs:read": "List organizations",
            "orgs:write": "Create organizations",
            "invitations:write": "Invite users to organization",
            "users:read": "List organization members",
            "users:write": "Manage organization members",
            "apps:read": "List applications",
            "apps:write": "Manage applications",
            "admin:users:read": "List all users (admin)",
            "admin:users:write": "Enable/disable users (admin)",
        },
        "roles": {
            "admin": ["profile:read", "profile:write", "orgs:read", "orgs:write", "invitations:write", "users:read", "users:write", "apps:read", "apps:write", "admin:users:read", "admin:users:write"],
            "member": ["profile:read", "profile:write", "orgs:read", "users:read", "apps:read"],
            "readonly": ["profile:read", "orgs:read", "users:read", "apps:read"],
        },
        "password_policy": {
            "min_length": settings.PASSWORD_MIN_LENGTH,
            "max_length": settings.PASSWORD_MAX_LENGTH,
            "require_uppercase": settings.PASSWORD_REQUIRE_UPPER,
            "require_lowercase": settings.PASSWORD_REQUIRE_LOWER,
            "require_digit": settings.PASSWORD_REQUIRE_DIGIT,
            "require_special": settings.PASSWORD_REQUIRE_SPECIAL,
        },
        "rate_limits": {
            "login": f"{settings.RATE_LIMIT_LOGIN_PER_MINUTE}/minute",
            "register": f"{settings.RATE_LIMIT_REGISTER_PER_HOUR}/hour",
            "password_reset": f"{settings.RATE_LIMIT_RESET_PER_HOUR}/hour",
            "global": f"{settings.RATE_LIMIT_GLOBAL_PER_MINUTE}/minute",
        },
        "oauth_providers": settings.OAUTH_PROVIDERS_ENABLED,
    }
