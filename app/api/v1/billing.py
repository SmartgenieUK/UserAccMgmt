from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.config import get_settings, Settings
from app.core.exceptions import ForbiddenError
from app.db.session import get_session
from app.models.enums import Role
from app.models.organization import Organization
from app.security.dependencies import get_current_user, get_current_membership, require_scopes
from app.services.audit_service import AuditService
from app.services.stripe_gateway import StripeGateway, get_stripe_gateway
from app.services.billing_service import BillingService

router = APIRouter()


class CheckoutSessionRequest(BaseModel):
    price_id: str


class UrlResponse(BaseModel):
    url: str


def _require_org(membership, org_id: str, admin: bool = False) -> None:
    if str(membership.org_id) != org_id:
        raise ForbiddenError("No membership for organization", code="org_mismatch")
    if admin and membership.role != Role.ADMIN:
        raise ForbiddenError("Admin role required", code="admin_required")


def get_billing_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    gateway: StripeGateway = Depends(get_stripe_gateway),
) -> BillingService:
    return BillingService(session, settings, AuditService(session, settings), gateway)


@router.post("/orgs/{org_id}/billing/checkout-session", response_model=UrlResponse)
async def create_checkout_session(
    org_id: str,
    data: CheckoutSessionRequest,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    service: BillingService = Depends(get_billing_service),
    _=Depends(require_scopes(["billing:write"])),
):
    _require_org(membership, org_id, admin=True)
    org = await session.get(Organization, uuid.UUID(org_id))
    url = await service.create_checkout_session(org, data.price_id, str(current_user.id))
    await session.commit()
    return UrlResponse(url=url)


@router.post("/orgs/{org_id}/billing/portal-session", response_model=UrlResponse)
async def create_portal_session(
    org_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    service: BillingService = Depends(get_billing_service),
    _=Depends(require_scopes(["billing:write"])),
):
    _require_org(membership, org_id, admin=True)
    org = await session.get(Organization, uuid.UUID(org_id))
    url = await service.create_portal_session(org, str(current_user.id))
    await session.commit()
    return UrlResponse(url=url)


@router.get("/orgs/{org_id}/billing")
async def get_billing_summary(
    org_id: str,
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    service: BillingService = Depends(get_billing_service),
    _=Depends(require_scopes(["entitlements:read"])),
):
    _require_org(membership, org_id)
    org = await session.get(Organization, uuid.UUID(org_id))
    summary = await service.billing_summary(org)
    return summary
