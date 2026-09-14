from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ValidationError
from app.db.session import get_session
from app.models.enums import ExternalProvider
from app.schemas.entra import EntraCalloutRequest, provide_claims_response
from app.security.entra_jwt import verify_callout_token
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.identity_link_service import IdentityLinkService

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

MAX_CLAIMS_BYTES = 3072  # Entra caps total claims at 3 KB (custom-claims-provider contract).


def _fit_budget(claims: dict) -> dict:
    """Keep the claims payload under Entra's 3 KB ceiling; the array-valued scope is the elastic part."""
    if len(json.dumps(claims).encode()) <= MAX_CLAIMS_BYTES:
        return claims
    trimmed = {**claims, "scope": []}
    return trimmed


@router.post("/entra/token-issuance")
async def entra_token_issuance(
    payload: EntraCalloutRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Entra custom-claims-provider endpoint (W3).

    A Tier-0 sign-in dependency: Entra calls this while minting a token, waits ~2s, and mints NO token if
    it fails. So it verifies the caller, resolves entitlement from one indexed path, makes no outbound
    calls, and returns entitlement claims as strings / string arrays only. An unmapped calling app yields
    an empty claims set (sign-in proceeds, no entitlement asserted); a DB fault propagates as 5xx so Entra
    fails closed rather than minting a token with no tier.
    """
    if credentials is None:
        raise AuthError("Missing bearer token", code="entra_no_bearer")
    verify_callout_token(settings, credentials.credentials)

    ctx = payload.data.authenticationContext
    email = ctx.user.mail or ctx.user.userPrincipalName
    if not email:
        raise ValidationError("Entra callout carried no email", code="entra_no_email")

    link = IdentityLinkService(session)
    user = await link.find_or_create_user(
        ExternalProvider.ENTRA_EXTERNAL, ctx.user.id, email, ctx.user.displayName
    )

    claims: dict = {}
    product = settings.ENTRA_PRODUCT_APP_IDS.get(ctx.clientServicePrincipal.appId or "")
    if product:
        membership = await link.primary_membership(user)
        service = EntitlementService(session, settings, AuditService(session, settings))
        entitlement = await service.resolve(str(membership.org_id), product)
        claims = _fit_budget(
            {
                "tier": str(entitlement.tier),
                "org_id": str(entitlement.org_id),
                "account_id": str(entitlement.account_id),
                "scope": [str(s) for s in entitlement.scope],
            }
        )

    await session.commit()
    return provide_claims_response(claims)
