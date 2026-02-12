from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthError, ConflictError
from app.core.plugins import PluginRegistry
from app.models import User, ExternalIdentity, Credential, Membership, Organization
from app.models.enums import ExternalProvider, Role
from app.security.hashing import hash_password
from app.security.permissions import resolve_scopes
from app.services.token_service import TokenService
from app.services.email_service import EmailService
from app.services.audit_service import AuditService
from app.services.oauth_providers import OAuthUserInfo
from app.utils.security import generate_pkce_pair, normalize_email
from app.utils.validation import slugify


@dataclass
class OAuthState:
    code_verifier: str
    redirect_uri: str


class OAuthStateStore:
    def __init__(self, redis, settings: Settings):
        self.redis = redis
        self.settings = settings
        self._memory: dict[str, OAuthState] = {}

    async def store(self, state: str, data: OAuthState) -> None:
        if self.redis:
            await self.redis.setex(
                f"oauth:state:{state}",
                self.settings.OAUTH_STATE_TTL_SECONDS,
                json.dumps({"code_verifier": data.code_verifier, "redirect_uri": data.redirect_uri}),
            )
            return
        self._memory[state] = data

    async def consume(self, state: str) -> OAuthState:
        if self.redis:
            raw = await self.redis.getdel(f"oauth:state:{state}")
            if not raw:
                raise AuthError("Invalid OAuth state", code="oauth_state_invalid")
            payload = json.loads(raw)
            return OAuthState(code_verifier=payload["code_verifier"], redirect_uri=payload["redirect_uri"])
        data = self._memory.pop(state, None)
        if not data:
            raise AuthError("Invalid OAuth state", code="oauth_state_invalid")
        return data


class OAuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        registry: PluginRegistry,
        token_service: TokenService,
        email_service: EmailService,
        audit_service: AuditService,
        redis=None,
    ):
        self.session = session
        self.settings = settings
        self.registry = registry
        self.token_service = token_service
        self.email_service = email_service
        self.audit_service = audit_service
        self.state_store = OAuthStateStore(redis, settings)

    async def authorization_url(self, provider_name: str, redirect_uri: str | None) -> tuple[str, str]:
        provider = self.registry.get_oauth_provider(provider_name)
        redirect = redirect_uri or self._default_redirect(provider_name)
        if not redirect:
            raise AuthError("Redirect URI not configured", code="oauth_redirect_missing")
        state = secrets.token_urlsafe(16)
        verifier, challenge = generate_pkce_pair()
        await self.state_store.store(state, OAuthState(code_verifier=verifier, redirect_uri=redirect))
        url = await provider.authorization_url(state=state, redirect_uri=redirect, code_challenge=challenge)
        return url, state

    async def callback(self, provider_name: str, code: str, state: str, redirect_uri: str | None):
        provider = self.registry.get_oauth_provider(provider_name)
        state_data = await self.state_store.consume(state)
        redirect = redirect_uri or state_data.redirect_uri
        token_data = await provider.exchange_code(code=code, redirect_uri=redirect, code_verifier=state_data.code_verifier)
        user_info: OAuthUserInfo = await provider.fetch_user_info(token_data)

        if not user_info.email or not user_info.email_verified:
            raise AuthError("Email not verified by provider", code="oauth_email_unverified")

        normalized = normalize_email(user_info.email)
        result = await self.session.execute(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == ExternalProvider(provider_name),
                ExternalIdentity.provider_user_id == user_info.sub,
            )
        )
        identity = result.scalar_one_or_none()

        if identity:
            user = await self.session.get(User, identity.user_id)
        else:
            result = await self.session.execute(select(User).where(User.normalized_email == normalized))
            user = result.scalar_one_or_none()
            if user:
                if not user.is_verified:
                    user.is_verified = True
                identity = ExternalIdentity(
                    user_id=user.id,
                    provider=ExternalProvider(provider_name),
                    provider_user_id=user_info.sub,
                    email=user_info.email,
                )
                self.session.add(identity)
            else:
                user = User(
                    email=user_info.email,
                    normalized_email=normalized,
                    display_name=user_info.name,
                    avatar_url=user_info.picture,
                    is_verified=True,
                )
                self.session.add(user)
                await self.session.flush()
                credential = Credential(user_id=user.id, password_hash=hash_password(secrets.token_urlsafe(32)))
                self.session.add(credential)
                identity = ExternalIdentity(
                    user_id=user.id,
                    provider=ExternalProvider(provider_name),
                    provider_user_id=user_info.sub,
                    email=user_info.email,
                )
                self.session.add(identity)

            await self._ensure_personal_org(user)

        await self.audit_service.log_event(
            action="oauth_login",
            user_id=str(user.id),
            metadata={"provider": provider_name},
        )

        membership = await self._get_primary_membership(user)
        scopes = resolve_scopes(membership.role)
        access_token, expires_in = await self.token_service.create_access_token(
            user_id=str(user.id),
            email=user.email,
            role=membership.role.value,
            org_id=str(membership.org_id),
            scopes=scopes,
        )
        refresh_token = await self.token_service.create_refresh_token(str(user.id), None, None)
        return access_token, refresh_token, expires_in

    async def _ensure_personal_org(self, user: User) -> None:
        result = await self.session.execute(select(Membership).where(Membership.user_id == user.id))
        membership = result.scalar_one_or_none()
        if membership:
            return
        name = f"{user.display_name or user.email}'s Org"
        org = Organization(name=name, slug=slugify(name))
        self.session.add(org)
        await self.session.flush()
        self.session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))

    async def _get_primary_membership(self, user: User) -> Membership:
        result = await self.session.execute(select(Membership).where(Membership.user_id == user.id))
        membership = result.scalar_one_or_none()
        if not membership:
            raise ConflictError("User has no organization", code="org_missing")
        return membership

    def _default_redirect(self, provider_name: str) -> str | None:
        if provider_name == "google":
            return self.settings.GOOGLE_REDIRECT_URI
        if provider_name == "microsoft":
            return self.settings.MICROSOFT_REDIRECT_URI
        return None
