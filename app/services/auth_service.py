from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthError, ConflictError, ValidationError
from app.core.hooks import HookManager
from app.models import User, Credential, VerificationToken, Membership, Organization, Role
from app.models.enums import VerificationTokenType
from app.security.hashing import hash_password, verify_password, hash_token, verify_token
from app.security.permissions import resolve_scopes
from app.services.token_service import TokenService
from app.services.email_service import EmailService
from app.services.audit_service import AuditService
from app.utils.security import normalize_email, generate_token_secret, split_token
from app.utils.time import utcnow
from app.utils.validation import slugify


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        hooks: HookManager,
        token_service: TokenService,
        email_service: EmailService,
        audit_service: AuditService,
    ):
        self.session = session
        self.settings = settings
        self.hooks = hooks
        self.token_service = token_service
        self.email_service = email_service
        self.audit_service = audit_service

    async def register(self, email: str, password: str, display_name: str | None, org_name: str | None) -> None:
        normalized = normalize_email(email)
        existing = await self.session.execute(select(User).where(User.normalized_email == normalized))
        if existing.scalar_one_or_none():
            raise ConflictError("Email already registered", code="email_exists")

        await self.hooks.run_email_domain_checks(email)
        await self.hooks.run_password_policy(password)

        user = User(
            email=email,
            normalized_email=normalized,
            display_name=display_name,
            is_verified=False,
            custom_schema_version=self.settings.PROFILE_SCHEMA_VERSION,
        )
        self.session.add(user)
        await self.session.flush()

        credential = Credential(user_id=user.id, password_hash=hash_password(password))
        self.session.add(credential)

        org = await self._create_default_org(user, org_name)
        self.session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))

        token = await self._create_verification_token(user, VerificationTokenType.EMAIL_VERIFY)

        await self.audit_service.log_event(
            action="user_registered",
            user_id=str(user.id),
            org_id=str(org.id),
        )

        await self.session.commit()
        await self.email_service.send_verification_email(user.email, token)

    async def verify_email(self, token: str) -> None:
        record = await self._consume_token(token, VerificationTokenType.EMAIL_VERIFY)
        user = await self.session.get(User, record.user_id)
        if not user:
            raise ValidationError("User not found", code="user_not_found")
        user.is_verified = True
        await self.audit_service.log_event(action="email_verified", user_id=str(user.id))
        await self.session.commit()

    async def login(self, email: str, password: str, org_id: str | None, ip: str | None, user_agent: str | None):
        normalized = normalize_email(email)
        result = await self.session.execute(
            select(User).options(selectinload(User.credential)).where(User.normalized_email == normalized)
        )
        user = result.scalar_one_or_none()
        if not user or not user.credential:
            await self.audit_service.log_event(action="login_failed", metadata={"email": email})
            raise AuthError("Invalid credentials", code="invalid_credentials")
        if not user.is_verified:
            raise AuthError("Email not verified", code="email_not_verified")

        credential = user.credential
        if credential.lockout_until and credential.lockout_until > utcnow():
            raise AuthError("Account locked. Try later.", code="account_locked")

        if not verify_password(password, credential.password_hash):
            await self._record_failed_login(credential)
            await self.audit_service.log_event(action="login_failed", user_id=str(user.id))
            raise AuthError("Invalid credentials", code="invalid_credentials")

        await self._clear_failed_login(credential)

        membership = await self._resolve_membership(user.id, org_id)
        scopes = resolve_scopes(membership.role)
        access_token, expires_in = await self.token_service.create_access_token(
            user_id=str(user.id),
            email=user.email,
            role=membership.role.value,
            org_id=str(membership.org_id),
            scopes=scopes,
        )
        refresh_token = await self.token_service.create_refresh_token(str(user.id), ip, user_agent)

        await self.audit_service.log_event(action="login_success", user_id=str(user.id), org_id=str(membership.org_id))
        await self.session.commit()

        return access_token, refresh_token, expires_in

    async def refresh(self, refresh_token: str, ip: str | None, user_agent: str | None):
        new_refresh = await self.token_service.rotate_refresh_token(refresh_token, ip, user_agent)
        refresh_obj = await self.token_service.verify_refresh_token(new_refresh)

        user = await self.session.get(User, refresh_obj.user_id)
        membership = await self._resolve_membership(user.id, None)
        scopes = resolve_scopes(membership.role)
        access_token, expires_in = await self.token_service.create_access_token(
            user_id=str(user.id),
            email=user.email,
            role=membership.role.value,
            org_id=str(membership.org_id),
            scopes=scopes,
        )
        await self.session.commit()
        return access_token, new_refresh, expires_in

    async def logout(self, refresh_token: str) -> None:
        await self.token_service.revoke_refresh_token(refresh_token)
        await self.session.commit()

    async def request_password_reset(self, email: str) -> None:
        normalized = normalize_email(email)
        result = await self.session.execute(select(User).where(User.normalized_email == normalized))
        user = result.scalar_one_or_none()
        if not user:
            return
        token = await self._create_verification_token(user, VerificationTokenType.PASSWORD_RESET)
        await self.session.commit()
        await self.email_service.send_password_reset_email(user.email, token)

    async def confirm_password_reset(self, token: str, new_password: str) -> None:
        await self.hooks.run_password_policy(new_password)
        record = await self._consume_token(token, VerificationTokenType.PASSWORD_RESET)
        user = await self.session.get(User, record.user_id)
        if not user or not user.credential:
            raise ValidationError("User not found", code="user_not_found")
        user.credential.password_hash = hash_password(new_password)
        user.credential.password_changed_at = utcnow()
        await self.token_service.revoke_all_tokens_for_user(str(user.id))
        await self.audit_service.log_event(action="password_reset", user_id=str(user.id))
        await self.session.commit()

    async def change_password(self, user: User, current_password: str, new_password: str) -> None:
        if not verify_password(current_password, user.credential.password_hash):
            raise AuthError("Invalid current password", code="invalid_password")
        await self.hooks.run_password_policy(new_password)
        user.credential.password_hash = hash_password(new_password)
        user.credential.password_changed_at = utcnow()
        await self.token_service.revoke_all_tokens_for_user(str(user.id))
        await self.audit_service.log_event(action="password_changed", user_id=str(user.id))
        await self.session.commit()

    async def request_email_change(self, user: User, new_email: str, current_password: str) -> None:
        if not verify_password(current_password, user.credential.password_hash):
            raise AuthError("Invalid password", code="invalid_password")
        normalized = normalize_email(new_email)
        existing = await self.session.execute(select(User).where(User.normalized_email == normalized))
        if existing.scalar_one_or_none():
            raise ConflictError("Email already in use", code="email_exists")

        await self.hooks.run_email_domain_checks(new_email)
        token = await self._create_verification_token(
            user, VerificationTokenType.EMAIL_CHANGE, email=new_email
        )
        await self.session.commit()
        await self.email_service.send_email_change_email(new_email, token)

    async def confirm_email_change(self, token: str) -> None:
        record = await self._consume_token(token, VerificationTokenType.EMAIL_CHANGE)
        user = await self.session.get(User, record.user_id)
        if not user or not record.email:
            raise ValidationError("Invalid email change token", code="email_change_invalid")
        user.email = record.email
        user.normalized_email = normalize_email(record.email)
        user.is_verified = True
        await self.audit_service.log_event(action="email_changed", user_id=str(user.id))
        await self.session.commit()

    async def _create_verification_token(
        self, user: User, token_type: VerificationTokenType, email: str | None = None
    ) -> str:
        secret = generate_token_secret(32)
        token_hash = hash_token(secret)
        expires_at = utcnow() + timedelta(
            hours={
                VerificationTokenType.EMAIL_VERIFY: self.settings.EMAIL_VERIFY_EXPIRE_HOURS,
                VerificationTokenType.PASSWORD_RESET: self.settings.PASSWORD_RESET_EXPIRE_HOURS,
                VerificationTokenType.EMAIL_CHANGE: self.settings.EMAIL_CHANGE_EXPIRE_HOURS,
            }[token_type]
        )
        record = VerificationToken(
            user_id=user.id,
            token_type=token_type,
            token_hash=token_hash,
            email=email,
            expires_at=expires_at,
        )
        self.session.add(record)
        await self.session.flush()
        return f"{record.id}.{secret}"

    async def _consume_token(self, token: str, token_type: VerificationTokenType) -> VerificationToken:
        token_id_str, secret = split_token(token)
        record = await self.session.get(VerificationToken, token_id_str)
        if not record or record.token_type != token_type:
            raise ValidationError("Invalid token", code="token_invalid")
        if record.used_at or record.expires_at <= utcnow():
            raise ValidationError("Token expired", code="token_expired")
        if not verify_token(secret, record.token_hash):
            raise ValidationError("Invalid token", code="token_invalid")
        record.used_at = utcnow()
        return record

    async def _resolve_membership(self, user_id: str, org_id: str | None) -> Membership:
        if org_id:
            result = await self.session.execute(
                select(Membership).where(Membership.user_id == user_id, Membership.org_id == org_id)
            )
            membership = result.scalar_one_or_none()
            if not membership:
                raise AuthError("No membership for organization", code="org_membership_missing")
            return membership
        result = await self.session.execute(select(Membership).where(Membership.user_id == user_id))
        membership = result.scalar_one_or_none()
        if not membership:
            raise AuthError("No organization membership", code="org_membership_missing")
        return membership

    async def _record_failed_login(self, credential: Credential) -> None:
        credential.failed_login_attempts += 1
        if credential.failed_login_attempts >= self.settings.LOCKOUT_THRESHOLD:
            credential.lockout_until = utcnow() + timedelta(minutes=self.settings.LOCKOUT_DURATION_MINUTES)

    async def _clear_failed_login(self, credential: Credential) -> None:
        credential.failed_login_attempts = 0
        credential.lockout_until = None
        credential.last_login_at = utcnow()

    async def _create_default_org(self, user: User, org_name: str | None) -> Organization:
        name = org_name or f"{user.display_name or user.email}'s Org"
        slug = slugify(name)
        org = Organization(name=name, slug=slug)
        self.session.add(org)
        await self.session.flush()
        return org
