from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthError, ConflictError
from app.core.plugins import PluginRegistry
from app.models import ExternalIdentity, Membership, User
from app.models.enums import ExternalProvider
from app.security.permissions import resolve_scopes
from app.services.audit_service import AuditService
from app.services.email_service import EmailService
from app.services.identity_link_service import IdentityLinkService
from app.services.oauth_providers import OAuthUserInfo
from app.services.rate_limit_service import RateLimiter
from app.services.token_service import TokenService
from app.utils.security import generate_pkce_pair, normalize_email

MAX_VERIFY_ATTEMPTS = 5  # per pending sign-in; the 6th attempt is refused even with the right code


@dataclass
class VerificationRequired:
    """The callback's answer when the provider did not verify the email: a code was sent, nothing was linked."""

    pending_id: str
    email_hint: str


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
            key = f"oauth:state:{state}"
            raw = await self.redis.get(key)
            if not raw:
                raise AuthError("Invalid OAuth state", code="oauth_state_invalid")
            await self.redis.delete(key)
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
        self.redis = redis
        self.state_store = OAuthStateStore(redis, settings)

    async def authorization_url(
        self, provider_name: str, redirect_uri: str | None, domain_hint: str | None = None
    ) -> tuple[str, str]:
        provider = self.registry.get_oauth_provider(provider_name)
        redirect = redirect_uri or self._default_redirect(provider_name)
        if not redirect:
            raise AuthError("Redirect URI not configured", code="oauth_redirect_missing")
        state = secrets.token_urlsafe(16)
        verifier, challenge = generate_pkce_pair()
        await self.state_store.store(state, OAuthState(code_verifier=verifier, redirect_uri=redirect))
        hint = {"domain_hint": domain_hint} if domain_hint else {}
        url = await provider.authorization_url(state=state, redirect_uri=redirect, code_challenge=challenge, **hint)
        return url, state

    async def callback(self, provider_name: str, code: str, state: str, redirect_uri: str | None):
        provider = self.registry.get_oauth_provider(provider_name)
        state_data = await self.state_store.consume(state)
        redirect = redirect_uri or state_data.redirect_uri
        token_data = await provider.exchange_code(code=code, redirect_uri=redirect, code_verifier=state_data.code_verifier)
        user_info: OAuthUserInfo = await provider.fetch_user_info(token_data)

        if not user_info.email:
            raise AuthError("Email not verified by provider", code="oauth_email_unverified")
        # Owner ruling 2026-10-16 (CR-208 certification F1): every sign-in needs a verified email. An identity
        # already linked signs in directly; otherwise an unverified email gets a one-time code and nothing is
        # created, linked or issued until POST /oauth/verify proves the address.
        if not user_info.email_verified and not await self._is_linked(provider_name, user_info.sub):
            return await self._start_verification(provider_name, user_info)
        return await self._sign_in(provider_name, user_info.sub, user_info.email, user_info.name, user_info.email_verified)

    async def verify(self, pending_id: str, otp: str):
        """Complete a pending sign-in with the emailed code: single use, 5 attempts, expires with the code."""
        key = self._pending_key(pending_id)
        raw = await self.redis.get(key) if self.redis else None
        if not raw:
            raise AuthError("Invalid or expired code", code="otp_invalid")
        attempts = await self.redis.incr(f"{key}:attempts")
        if attempts == 1:
            await self.redis.expire(f"{key}:attempts", self.settings.OTP_EXPIRE_MINUTES * 60)
        pending = json.loads(raw)
        if attempts > MAX_VERIFY_ATTEMPTS or not hmac.compare_digest(otp.encode(), pending["otp"].encode()):
            if attempts >= MAX_VERIFY_ATTEMPTS:
                await self.redis.delete(key)
            # Audit the refusal durably BEFORE raising: this path never reaches _sign_in's commit (BRIEF-008).
            # The attempt number only — never the code, the pending id or the address.
            action = "oauth_verification_locked" if attempts >= MAX_VERIFY_ATTEMPTS else "oauth_verification_failed"
            await self.audit_service.log_event(action=action, metadata={"provider": pending["provider"], "attempt": attempts})
            await self.session.commit()
            raise AuthError("Invalid or expired code", code="otp_invalid")
        if not await self.redis.delete(key):  # a concurrent verify already used it
            raise AuthError("Invalid or expired code", code="otp_invalid")
        existing = (
            await self.session.execute(select(User.id).where(User.normalized_email == normalize_email(pending["email"])))
        ).first()
        return await self._sign_in(
            pending["provider"], pending["provider_user_id"], pending["email"], pending["display_name"], True,
            via="email_code", outcome="joined" if existing else "created",
        )

    async def _is_linked(self, provider_name: str, provider_user_id: str) -> bool:
        result = await self.session.execute(
            select(ExternalIdentity.id).where(
                ExternalIdentity.provider == ExternalProvider(provider_name),
                ExternalIdentity.provider_user_id == provider_user_id,
            )
        )
        return result.first() is not None

    async def _start_verification(self, provider_name: str, user_info: OAuthUserInfo) -> VerificationRequired:
        if not self.redis:
            raise AuthError("Email verification unavailable", code="otp_unavailable")
        await RateLimiter(self.redis).hit(
            f"email_recipient:{normalize_email(user_info.email)}",
            self.settings.RATE_LIMIT_EMAIL_PER_RECIPIENT_PER_HOUR,
            3600,
        )
        pending_id = secrets.token_urlsafe(32)
        otp = self.email_service.generate_otp()
        pending = {
            "provider": provider_name,
            "provider_user_id": user_info.sub,
            "email": user_info.email,
            "display_name": user_info.name,
            "otp": otp,
        }
        await self.redis.setex(self._pending_key(pending_id), self.settings.OTP_EXPIRE_MINUTES * 60, json.dumps(pending))
        await self.email_service.send_verification_email(user_info.email, otp)
        # Durable audit (BRIEF-008): this 202 path never reaches _sign_in's commit. The address is hashed, never stored.
        await self.audit_service.log_event(
            action="oauth_verification_sent",
            metadata={"provider": provider_name, "email_hash": hashlib.sha256(normalize_email(user_info.email).encode()).hexdigest()},
        )
        await self.session.commit()
        local, _, domain = user_info.email.partition("@")
        return VerificationRequired(pending_id=pending_id, email_hint=f"{local[:1]}•••@{domain}")

    @staticmethod
    def _pending_key(pending_id: str) -> str:
        return f"oauth:pending:{pending_id}"

    async def _sign_in(
        self,
        provider_name: str,
        provider_user_id: str,
        email: str,
        display_name: str | None,
        email_verified: bool,
        via: str = "provider",
        outcome: str | None = None,
    ):
        user = await IdentityLinkService(self.session).find_or_create_user(
            provider=ExternalProvider(provider_name),
            provider_user_id=provider_user_id,
            email=email,
            display_name=display_name,
            email_verified=email_verified,
        )

        await self.audit_service.log_event(
            action="oauth_login",
            user_id=str(user.id),
            metadata={"provider": provider_name, "email_verified_by": via},
        )
        if outcome:  # a code-verified sign-in: did it join an existing account or create one
            await self.audit_service.log_event(
                action="oauth_verification_completed",
                user_id=str(user.id),
                metadata={"provider": provider_name, "outcome": outcome},
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
        # Save the user, the provider link and the refresh token before the tokens go out, as login/register
        # do: get_session closes without committing (CR-208 certification round 2, R2-F1).
        await self.session.commit()
        return access_token, refresh_token, expires_in

    async def _get_primary_membership(self, user: User) -> Membership:
        # Earliest membership is the default org (plan D2, 2026-09-14).
        result = await self.session.execute(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at, Membership.id)
        )
        membership = result.scalars().first()
        if not membership:
            raise ConflictError("User has no organization", code="org_missing")
        return membership

    def _default_redirect(self, provider_name: str) -> str | None:
        if provider_name == "google":
            return self.settings.GOOGLE_REDIRECT_URI
        if provider_name == "microsoft":
            return self.settings.MICROSOFT_REDIRECT_URI
        if provider_name == "entra_external":
            return self.settings.ENTRA_REDIRECT_URI
        return None
