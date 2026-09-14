from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExternalIdentity, Membership, Organization, User
from app.models.enums import ExternalProvider, Role
from app.utils.security import normalize_email
from app.utils.validation import slugify


class IdentityLinkService:
    """Resolves an external identity to a local user, creating one just-in-time.

    Precedence: existing (provider, subject) link -> existing user by verified email (link it) -> a new
    user with a personal org. This is the shared path for federated sign-in; the Entra claims provider (W3)
    is its first caller. Callers commit; this only adds/flushes.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def find_or_create_user(
        self,
        provider: ExternalProvider,
        provider_user_id: str,
        email: str,
        display_name: str | None = None,
        email_verified: bool = True,
    ) -> User:
        result = await self.session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == provider,
                ExternalIdentity.provider_user_id == provider_user_id,
            )
        )
        identity = result.scalar_one_or_none()
        if identity:
            return await self.session.get(User, identity.user_id)

        normalized = normalize_email(email)
        user = (
            await self.session.execute(select(User).where(User.normalized_email == normalized))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                normalized_email=normalized,
                display_name=display_name,
                is_verified=email_verified,
            )
            self.session.add(user)
            await self.session.flush()
        elif email_verified and not user.is_verified:
            user.is_verified = True

        self.session.add(
            ExternalIdentity(
                user_id=user.id,
                provider=provider,
                provider_user_id=provider_user_id,
                email=email,
            )
        )
        await self._ensure_personal_org(user)
        await self.session.flush()
        return user

    async def _ensure_personal_org(self, user: User) -> None:
        existing = await self.session.execute(select(Membership).where(Membership.user_id == user.id))
        if existing.scalars().first():
            return
        name = f"{user.display_name or user.email}'s Org"
        org = Organization(name=name, slug=slugify(name))
        self.session.add(org)
        await self.session.flush()
        self.session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))
        await self.session.flush()

    async def primary_membership(self, user: User) -> Membership | None:
        # Earliest membership is the default org (plan D2, 2026-09-14).
        result = await self.session.execute(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at, Membership.id)
        )
        return result.scalars().first()
