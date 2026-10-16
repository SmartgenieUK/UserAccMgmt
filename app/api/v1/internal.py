from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, NotFoundError
from app.db.session import get_session
from app.models import ExternalIdentity, User
from app.models.enums import ExternalProvider
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.identity_link_service import IdentityLinkService

router = APIRouter()

# Lookup-interface provider name -> the provider the link row was written under.
_PROVIDERS = {"entra": ExternalProvider.ENTRA_EXTERNAL}


@router.get("/internal/entitlements/by-subject")
async def entitlement_by_subject(
    provider: str = Query(...),
    oid: str = Query(...),
    product: str = Query(...),
    x_service_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Service-to-service: what has the person linked to this Entra oid paid for (CR-208 work item C).

    DevGenie's server calls this with a verified sign-in's `oid`. Unset DEVGENIE_SERVICE_SECRET -> 404 to
    everyone; a wrong or missing secret -> 401 with no detail (constant-time compare). An oid with no
    entra_external link (or a deactivated user) -> 404; this path never creates a user. Tier/status/expiry come from
    EntitlementService.resolve on the person's primary org; account_id is the person's UAM user id, so each
    person is their own audit actor in DevGenie. Read-only.
    """
    expected = settings.DEVGENIE_SERVICE_SECRET
    if not expected:
        raise NotFoundError()
    if not hmac.compare_digest((x_service_secret or "").encode(), expected.encode()):
        raise AuthError()

    link_provider = _PROVIDERS.get(provider)
    if link_provider is None:
        raise NotFoundError()
    identity = (
        await session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == link_provider, ExternalIdentity.provider_user_id == oid
            )
        )
    ).scalar_one_or_none()
    user = await session.get(User, identity.user_id) if identity else None
    if user is not None and not user.is_active:
        user = None  # a deactivated account answers like an unknown one — never a paid tier
    membership = await IdentityLinkService(session).primary_membership(user) if user else None
    if membership is None:
        raise NotFoundError()

    entitlement = await EntitlementService(session, settings, AuditService(session, settings)).resolve(
        str(membership.org_id), product
    )
    return {
        "tier": entitlement.tier,
        "status": entitlement.status,
        "expires_at": entitlement.expires_at.isoformat() if entitlement.expires_at else None,
        "org_id": str(membership.org_id),
        "account_id": str(user.id),
    }
