from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_registry
from app.core.config import get_settings
from app.core.exceptions import ValidationError
from app.db.session import get_session
from app.schemas.auth import OAuthAuthorizeResponse, OAuthCallbackRequest, OAuthVerifyRequest
from app.schemas.token import TokenPair
from app.services.oauth_service import OAuthService, VerificationRequired
from app.services.token_service import TokenService
from app.services.email_service import EmailService
from app.services.audit_service import AuditService
from app.services.oauth_providers import GoogleProvider, MicrosoftProvider, EntraExternalProvider

router = APIRouter()

# Standard OAuth `domain_hint` values the authorize route passes through (Entra: skip to that federated IdP).
ALLOWED_DOMAIN_HINTS = {"google"}


@router.get("/oauth/{provider}/authorize", response_model=OAuthAuthorizeResponse)
async def oauth_authorize(
    provider: str,
    request: Request,
    domain_hint: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_registry),
):
    registry.register_oauth_provider(GoogleProvider(settings))
    registry.register_oauth_provider(MicrosoftProvider(settings))
    if settings.ENTRA_TENANT_DOMAIN and settings.ENTRA_CLIENT_ID:
        registry.register_oauth_provider(EntraExternalProvider(settings))
    service = OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
        redis=request.app.state.redis,
    )
    if domain_hint is not None and domain_hint not in ALLOWED_DOMAIN_HINTS:
        raise ValidationError("domain_hint not allowed", code="domain_hint_not_allowed")
    url, state = await service.authorization_url(provider, None, domain_hint)
    return OAuthAuthorizeResponse(authorization_url=url, state=state)


@router.post("/oauth/{provider}/callback", response_model=TokenPair)
async def oauth_callback(
    provider: str,
    data: OAuthCallbackRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_registry),
):
    registry.register_oauth_provider(GoogleProvider(settings))
    registry.register_oauth_provider(MicrosoftProvider(settings))
    if settings.ENTRA_TENANT_DOMAIN and settings.ENTRA_CLIENT_ID:
        registry.register_oauth_provider(EntraExternalProvider(settings))
    service = OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
        redis=request.app.state.redis,
    )
    result = await service.callback(
        provider_name=provider,
        code=data.code,
        state=data.state,
        redirect_uri=data.redirect_uri,
    )
    if isinstance(result, VerificationRequired):
        # The provider did not verify the email: a code was sent; finish with POST /oauth/verify.
        return JSONResponse(
            status_code=202,
            content={"status": "verification_required", "pending_id": result.pending_id, "email_hint": result.email_hint},
        )
    access, refresh, expires_in = result
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/oauth/verify", response_model=TokenPair)
async def oauth_verify(
    data: OAuthVerifyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_registry),
):
    service = OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
        redis=request.app.state.redis,
    )
    access, refresh, expires_in = await service.verify(data.pending_id, data.otp)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)
