from __future__ import annotations

import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.core.plugins import PluginRegistry
from app.models import Credential, ExternalIdentity, Membership, Organization, User
from app.models.enums import ExternalProvider
from app.security.hashing import hash_password
from app.services.audit_service import AuditService
from app.services.email_service import EmailService
from app.services.oauth_providers import OAuthUserInfo
from app.services.oauth_service import OAuthService
from app.services.token_service import TokenService


class FakeOAuthProvider:
    def __init__(self, name: str = "google", user_info: OAuthUserInfo | None = None):
        self.name = name
        self.user_info = user_info

    async def authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None = None) -> str:
        return f"https://accounts.google.com/o/oauth2/auth?state={state}&redirect_uri={redirect_uri}"

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> dict:
        return {}

    async def fetch_user_info(self, token_data: dict) -> OAuthUserInfo:
        assert self.user_info is not None, "user_info must be set"
        return self.user_info


def _build_oauth_service(session, provider: FakeOAuthProvider) -> OAuthService:
    settings = get_settings()
    registry = PluginRegistry()
    registry.register_oauth_provider(provider)
    token_service = TokenService(session, settings)
    email_service = EmailService(settings)
    audit_service = AuditService(session, settings)
    return OAuthService(
        session=session,
        settings=settings,
        registry=registry,
        token_service=token_service,
        email_service=email_service,
        audit_service=audit_service,
        redis=None,
    )


async def test_oauth_new_user_creates_no_credential(db_session):
    email = "newuser@example.com"
    sub = "google-sub-new-1"
    provider = FakeOAuthProvider(
        name="google",
        user_info=OAuthUserInfo(
            sub=sub,
            email=email,
            email_verified=True,
            name="New User",
            picture=None,
        ),
    )
    service = _build_oauth_service(db_session, provider)

    url, state = await service.authorization_url("google", "http://cb")
    assert "state=" in url

    res = await service.callback("google", code="x", state=state, redirect_uri="http://cb")
    assert isinstance(res, tuple)
    assert len(res) == 3
    access_token, refresh_token, expires_in = res
    assert access_token and refresh_token and expires_in > 0

    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    assert user is not None
    assert user.is_verified is True

    identity = (
        await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))
    ).scalar_one_or_none()
    assert identity is not None
    assert identity.provider == ExternalProvider.GOOGLE
    assert identity.provider_user_id == sub

    membership = (
        await db_session.execute(select(Membership).where(Membership.user_id == user.id))
    ).scalar_one_or_none()
    assert membership is not None

    cred = (await db_session.execute(select(Credential).where(Credential.user_id == user.id))).scalar_one_or_none()
    assert cred is None


async def test_oauth_claims_unverified_account_and_drops_credential(db_session):
    email = "victim@example.com"
    squatter = User(
        email=email,
        normalized_email=email,
        is_verified=False,
    )
    db_session.add(squatter)
    await db_session.flush()

    cred = Credential(user_id=squatter.id, password_hash=hash_password("SquatterPass1!"))
    db_session.add(cred)
    await db_session.flush()

    sub = "google-sub-victim-1"
    provider = FakeOAuthProvider(
        name="google",
        user_info=OAuthUserInfo(
            sub=sub,
            email=email,
            email_verified=True,
            name="Victim User",
            picture=None,
        ),
    )
    service = _build_oauth_service(db_session, provider)

    url, state = await service.authorization_url("google", "http://cb")
    res = await service.callback("google", code="x", state=state, redirect_uri="http://cb")
    assert len(res) == 3

    claimed_user = (await db_session.execute(select(User).where(User.id == squatter.id))).scalar_one()
    assert claimed_user.is_verified is True

    identity = (
        await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider_user_id == sub))
    ).scalar_one_or_none()
    assert identity is not None
    assert identity.user_id == squatter.id
    assert identity.provider == ExternalProvider.GOOGLE

    dropped_cred = (
        await db_session.execute(select(Credential).where(Credential.user_id == squatter.id))
    ).scalar_one_or_none()
    assert dropped_cred is None


async def test_oauth_existing_link_returns_same_user(db_session):
    email = "repeat@example.com"
    sub = "google-sub-repeat-1"
    provider = FakeOAuthProvider(
        name="google",
        user_info=OAuthUserInfo(
            sub=sub,
            email=email,
            email_verified=True,
            name="Repeat User",
            picture=None,
        ),
    )
    service = _build_oauth_service(db_session, provider)

    # First callback
    url1, state1 = await service.authorization_url("google", "http://cb")
    res1 = await service.callback("google", code="x", state=state1, redirect_uri="http://cb")
    assert len(res1) == 3

    user1 = (await db_session.execute(select(User).where(User.email == email))).scalar_one()

    # Second callback with same provider sub
    url2, state2 = await service.authorization_url("google", "http://cb")
    res2 = await service.callback("google", code="y", state=state2, redirect_uri="http://cb")
    assert len(res2) == 3

    # Decode tokens to verify same user id returned both times
    payload1 = jwt.decode(res1[0], options={"verify_signature": False})
    payload2 = jwt.decode(res2[0], options={"verify_signature": False})
    assert payload1["sub"] == payload2["sub"] == str(user1.id)

    # Verify single user, single external identity, single personal org membership
    users = (await db_session.execute(select(User).where(User.email == email))).scalars().all()
    assert len(users) == 1
    assert users[0].id == user1.id

    identities = (
        await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider_user_id == sub))
    ).scalars().all()
    assert len(identities) == 1
    assert identities[0].user_id == user1.id

    memberships = (
        await db_session.execute(select(Membership).where(Membership.user_id == user1.id))
    ).scalars().all()
    assert len(memberships) == 1

    orgs = (
        await db_session.execute(
            select(Organization).join(Membership, Membership.org_id == Organization.id).where(Membership.user_id == user1.id)
        )
    ).scalars().all()
    assert len(orgs) == 1
