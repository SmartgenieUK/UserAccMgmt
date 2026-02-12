from __future__ import annotations

from datetime import timedelta
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, ValidationError
from app.models import Organization, Membership, Invitation, Role
from app.security.hashing import hash_token, verify_token
from app.services.email_service import EmailService
from app.utils.security import split_token
from app.utils.time import utcnow
from app.utils.validation import slugify


class OrgService:
    def __init__(self, session: AsyncSession, settings: Settings, email_service: EmailService):
        self.session = session
        self.settings = settings
        self.email_service = email_service

    async def create_org(self, user_id: str, name: str, slug: str | None) -> Organization:
        slug_value = slugify(slug or name)
        existing = await self.session.execute(select(Organization).where(Organization.slug == slug_value))
        if existing.scalar_one_or_none():
            raise ConflictError("Organization slug already exists", code="org_slug_exists")
        org = Organization(name=name, slug=slug_value)
        self.session.add(org)
        await self.session.flush()
        self.session.add(Membership(user_id=user_id, org_id=org.id, role=Role.ADMIN))
        return org

    async def list_orgs(self, user_id: str) -> list[Organization]:
        result = await self.session.execute(
            select(Organization).join(Membership).where(Membership.user_id == user_id)
        )
        return list(result.scalars().all())

    async def invite(self, org_id: str, inviter_user_id: str, email: str, role: Role) -> None:
        secret = secrets.token_urlsafe(32)
        token_hash = hash_token(secret)
        expires_at = utcnow() + timedelta(days=7)
        invitation = Invitation(
            org_id=org_id,
            inviter_user_id=inviter_user_id,
            email=email,
            role=role,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self.session.add(invitation)
        await self.session.flush()
        token = f"{invitation.id}.{secret}"

        org = await self.session.get(Organization, org_id)
        if not org:
            raise ValidationError("Organization not found", code="org_not_found")
        await self.email_service.send_invitation_email(email, org.name, token)

    async def accept_invitation(self, token: str, user_id: str, user_email: str) -> Organization:
        token_id_str, secret = split_token(token)
        invitation = await self.session.get(Invitation, token_id_str)
        if not invitation:
            raise ValidationError("Invalid invitation token", code="invite_invalid")
        if invitation.accepted_at or invitation.expires_at <= utcnow():
            raise ValidationError("Invitation expired", code="invite_expired")
        if invitation.email.lower() != user_email.lower():
            raise ValidationError("Invitation email mismatch", code="invite_email_mismatch")
        if not verify_token(secret, invitation.token_hash):
            raise ValidationError("Invalid invitation token", code="invite_invalid")

        existing = await self.session.execute(
            select(Membership).where(Membership.user_id == user_id, Membership.org_id == invitation.org_id)
        )
        if not existing.scalar_one_or_none():
            self.session.add(
                Membership(user_id=user_id, org_id=invitation.org_id, role=invitation.role)
            )

        invitation.accepted_at = utcnow()
        org = await self.session.get(Organization, invitation.org_id)
        if not org:
            raise ValidationError("Organization not found", code="org_not_found")
        return org
