from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.application import (
    ApplicationCreate,
    ApplicationRead,
    ApplicationCreated,
    ApplicationUpdate,
    RotateSecretResponse,
)
from app.schemas.common import MessageResponse
from app.security.dependencies import get_current_user, get_current_membership, require_scopes
from app.services.application_service import ApplicationService
from app.models.enums import Role

router = APIRouter()


@router.post("/orgs/{org_id}/apps", response_model=ApplicationCreated, status_code=201)
async def create_application(
    org_id: str,
    data: ApplicationCreate,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:write"])),
):
    if str(membership.org_id) != org_id or membership.role != Role.ADMIN:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Admin role required", code="admin_required")

    service = ApplicationService(session, settings)
    app, client_secret = await service.create(org_id, data.name, data.redirect_uris, data.allowed_scopes)
    await session.commit()

    return ApplicationCreated(
        id=app.id,
        org_id=app.org_id,
        name=app.name,
        client_id=app.client_id,
        client_secret=client_secret,
        redirect_uris=app.redirect_uris,
        allowed_scopes=app.allowed_scopes,
        is_active=app.is_active,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


@router.get("/orgs/{org_id}/apps", response_model=list[ApplicationRead])
async def list_applications(
    org_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:read"])),
):
    if str(membership.org_id) != org_id:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Not a member of this organization", code="org_mismatch")

    service = ApplicationService(session, settings)
    apps = await service.list_by_org(org_id)
    return apps


@router.get("/orgs/{org_id}/apps/{app_id}", response_model=ApplicationRead)
async def get_application(
    org_id: str,
    app_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:read"])),
):
    if str(membership.org_id) != org_id:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Not a member of this organization", code="org_mismatch")

    service = ApplicationService(session, settings)
    return await service.get(app_id, org_id)


@router.patch("/orgs/{org_id}/apps/{app_id}", response_model=ApplicationRead)
async def update_application(
    org_id: str,
    app_id: str,
    data: ApplicationUpdate,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:write"])),
):
    if str(membership.org_id) != org_id or membership.role != Role.ADMIN:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Admin role required", code="admin_required")

    service = ApplicationService(session, settings)
    app = await service.update(
        app_id, org_id,
        name=data.name,
        redirect_uris=data.redirect_uris,
        allowed_scopes=data.allowed_scopes,
        is_active=data.is_active,
    )
    await session.commit()
    return app


@router.post("/orgs/{org_id}/apps/{app_id}/rotate-secret", response_model=RotateSecretResponse)
async def rotate_application_secret(
    org_id: str,
    app_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:write"])),
):
    if str(membership.org_id) != org_id or membership.role != Role.ADMIN:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Admin role required", code="admin_required")

    service = ApplicationService(session, settings)
    app, new_secret = await service.rotate_secret(app_id, org_id)
    await session.commit()
    return RotateSecretResponse(client_id=app.client_id, client_secret=new_secret)


@router.delete("/orgs/{org_id}/apps/{app_id}", response_model=MessageResponse)
async def delete_application(
    org_id: str,
    app_id: str,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["apps:write"])),
):
    if str(membership.org_id) != org_id or membership.role != Role.ADMIN:
        from app.core.exceptions import ForbiddenError
        raise ForbiddenError("Admin role required", code="admin_required")

    service = ApplicationService(session, settings)
    await service.delete(app_id, org_id)
    await session.commit()
    return MessageResponse(message="Application deleted")
