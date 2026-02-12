from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_registry
from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.auth import OAuthAuthorizeResponse, OAuthCallbackRequest
from app.schemas.token import TokenPair
from app.services.oauth_service import OAuthService
from app.services.token_service import TokenService
from app.services.email_service import EmailService
from app.services.audit_service import AuditService
from app.services.oauth_providers import GoogleProvider, MicrosoftProvider

router = APIRouter()


@router.get("/oauth/{provider}/authorize", response_model=OAuthAuthorizeResponse)
async def oauth_authorize(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    registry=Depends(get_registry),
):
    registry.register_oauth_provider(GoogleProvider(settings))
    registry.register_oauth_provider(MicrosoftProvider(settings))
    service = OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
        redis=request.app.state.redis,
    )
    url, state = await service.authorization_url(provider, None)
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
    service = OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=TokenService(session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(session, settings),
        redis=request.app.state.redis,
    )
    access, refresh, expires_in = await service.callback(
        provider_name=provider,
        code=data.code,
        state=data.state,
        redirect_uri=data.redirect_uri,
    )
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in)
