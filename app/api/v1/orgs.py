from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.org import OrganizationCreate, OrganizationRead, InviteRequest, InviteResponse, InvitationAcceptRequest
from app.schemas.common import MessageResponse
from app.security.dependencies import get_current_user, get_current_membership, require_scopes
from app.services.org_service import OrgService
from app.services.email_service import EmailService
from app.models.enums import Role

router = APIRouter()


@router.post("/orgs", response_model=OrganizationRead)
async def create_org(
    data: OrganizationCreate,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["orgs:write"])),
):
    service = OrgService(session, settings, EmailService(settings))
    org = await service.create_org(str(current_user.id), data.name, data.slug)
    await session.commit()
    return org


@router.get("/orgs", response_model=list[OrganizationRead])
async def list_orgs(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["orgs:read"])),
):
    service = OrgService(session, settings, EmailService(settings))
    orgs = await service.list_orgs(str(current_user.id))
    return orgs


@router.post("/orgs/{org_id}/invite", response_model=InviteResponse)
async def invite_to_org(
    org_id: str,
    data: InviteRequest,
    current_user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
    _=Depends(require_scopes(["invitations:write"])),
):
    if str(membership.org_id) != org_id or membership.role != Role.ADMIN:
        return InviteResponse(message="Admin role required")
    service = OrgService(session, settings, EmailService(settings))
    await service.invite(org_id, str(current_user.id), data.email, Role(data.role))
    await session.commit()
    return InviteResponse(message="Invitation sent")


@router.post("/invitations/accept", response_model=MessageResponse)
async def accept_invitation(
    data: InvitationAcceptRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings=Depends(get_settings),
):
    service = OrgService(session, settings, EmailService(settings))
    await service.accept_invitation(data.token, str(current_user.id), current_user.email)
    await session.commit()
    return MessageResponse(message="Invitation accepted")
