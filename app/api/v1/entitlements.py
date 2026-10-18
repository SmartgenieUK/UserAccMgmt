from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, NotFoundError
from app.db.session import get_session
from app.models import Organization
from app.models.enums import Role
from app.schemas.entitlement import EntitlementRead, EntitlementOverride, SeatAssignRequest
from app.security.dependencies import get_current_user, get_current_membership, require_platform_operator, require_scopes
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService

router = APIRouter()


def _service(session: AsyncSession, settings) -> EntitlementService:
    return EntitlementService(session, settings, AuditService(session, settings))


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _require_org(membership, org_id: str, admin: bool = False) -> None:
    if str(membership.org_id) != org_id:
        raise ForbiddenError("No membership for organization", code="org_mismatch")
    if admin and membership.role != Role.ADMIN:
        raise ForbiddenError("Admin role required", code="admin_required")


@router.get("/entitlements/me", response_model=EntitlementRead)
async def my_entitlement(
    product: str = Query(...),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["entitlements:read"])),
):
    return (await _service(session, settings).resolve(str(membership.org_id), product)).as_dict()


@router.get("/orgs/{org_id}/entitlements", response_model=list[EntitlementRead])
async def org_entitlements(
    org_id: str,
    product: str | None = Query(None),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["entitlements:read"])),
):
    _require_org(membership, org_id)
    service = _service(session, settings)
    products = [product] if product else await service.products()
    return [(await service.resolve(org_id, p)).as_dict() for p in products]


@router.put("/orgs/{org_id}/entitlements/{product}", response_model=EntitlementRead)
async def override_entitlement(
    org_id: str,
    product: str,
    data: EntitlementOverride,
    _=Depends(require_platform_operator()),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
):
    # Platform operator only (BUG-06): an org admin setting their own plan would hand out paid tiers.
    # Cross-organisation by purpose, so no membership check.
    if await session.get(Organization, _uuid_or_none(org_id)) is None:
        raise NotFoundError("Organization not found", code="org_not_found")
    entitlement = await _service(session, settings).override(
        org_id, product, data.tier, data.seats, data.expires_at, actor_user_id=str(current_user.id)
    )
    await session.commit()
    return entitlement.as_dict()


@router.post("/orgs/{org_id}/entitlements/{product}/seats", response_model=EntitlementRead)
async def assign_seat(
    org_id: str,
    product: str,
    data: SeatAssignRequest,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["billing:write"])),
):
    _require_org(membership, org_id, admin=True)
    entitlement = await _service(session, settings).assign_seat(org_id, product, data.user_id, actor_user_id=str(current_user.id))
    await session.commit()
    return entitlement.as_dict()


@router.delete("/orgs/{org_id}/entitlements/{product}/seats/{user_id}", response_model=EntitlementRead)
async def unassign_seat(
    org_id: str,
    product: str,
    user_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["billing:write"])),
):
    _require_org(membership, org_id, admin=True)
    entitlement = await _service(session, settings).unassign_seat(org_id, product, user_id, actor_user_id=str(current_user.id))
    await session.commit()
    return entitlement.as_dict()
