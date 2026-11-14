from __future__ import annotations

import types
import pytest

from app.core.plugins import PluginRegistry
from app.services.oauth_providers import EntraExternalProvider


async def test_oauth_entra_external_provider():
    settings = types.SimpleNamespace(
        ENTRA_TENANT_DOMAIN="testtenant",
        ENTRA_TENANT_ID="tenant-123-abc",
        ENTRA_CLIENT_ID="client-456-def",
        ENTRA_CLIENT_SECRET="secret-789-ghi",
    )
    registry = PluginRegistry()
    provider = EntraExternalProvider(settings)  # type: ignore[arg-type]
    registry.register_oauth_provider(provider)

    retrieved = registry.get_oauth_provider("entra_external")
    assert isinstance(retrieved, EntraExternalProvider)
    assert retrieved is provider
    assert retrieved.name == "entra_external"
    assert retrieved.client_id == "client-456-def"
    assert retrieved.client_secret == "secret-789-ghi"

    url = await provider.authorization_url(state="s", redirect_uri="http://cb", code_challenge="c")
    assert isinstance(url, str)
    assert "ciamlogin.com" in url
    assert "tenant-123-abc" in url
    assert "code_challenge_method=S256" in url


# ---------------------------------------------------------------------------------------------------------
# CR-208 work item B: a portal sign-in through entra_external links the person by the VERIFIED ID token's
# oid (the user object id, the same in every app of the tenant), never by Graph userinfo's pairwise sub.
# ---------------------------------------------------------------------------------------------------------
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import AuthError
from app.models import ExternalIdentity, User
from app.models.enums import ExternalProvider
from app.services.audit_service import AuditService
from app.services.email_service import EmailService
from app.services.oauth_providers import BaseOAuthProvider, OAuthUserInfo
from app.services.oauth_service import OAuthService, OAuthState
from app.services.token_service import TokenService

TENANT_ID = "tenant-123-abc"
ISSUER = f"https://{TENANT_ID}.ciamlogin.com/{TENANT_ID}/v2.0"
CLIENT_ID = "client-456-def"
PAIRWISE_SUB = "pairwise-sub-for-the-portal-app"
OID = "7d2f6c1e-0000-4000-8000-00000000bbbb"
EMAIL = "grace@example.com"


@pytest.fixture()
def entra_provider_settings(rsa_keypair):
    return get_settings().model_copy(
        update={
            "ENTRA_TENANT_DOMAIN": "testtenant",
            "ENTRA_TENANT_ID": TENANT_ID,
            "ENTRA_CLIENT_ID": CLIENT_ID,
            "ENTRA_CLIENT_SECRET": "secret-789-ghi",
            "ENTRA_ISSUER": ISSUER,
            "ENTRA_SIGNING_PUBLIC_KEY_PEM": rsa_keypair[1].decode(),
        }
    )


def _id_token(private_pem, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": PAIRWISE_SUB, "oid": OID, "iat": now, "exp": now + 300}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "k1"})


def _other_private_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


async def _sign_in(db_session, settings, monkeypatch, token_data):
    """Drive OAuthService.callback through the REAL EntraExternalProvider; only the two network calls
    (the code exchange and Graph userinfo) are stubbed."""
    provider = EntraExternalProvider(settings)

    async def fake_exchange(code, redirect_uri, code_verifier):
        return token_data

    async def fake_graph_userinfo(self, token_data):
        return OAuthUserInfo(sub=PAIRWISE_SUB, email=EMAIL, email_verified=True, name="Grace", picture=None)

    monkeypatch.setattr(provider, "exchange_code", fake_exchange)
    monkeypatch.setattr(BaseOAuthProvider, "fetch_user_info", fake_graph_userinfo)
    registry = PluginRegistry()
    registry.register_oauth_provider(provider)
    service = OAuthService(
        session=db_session,
        settings=settings,
        registry=registry,
        token_service=TokenService(db_session, settings),
        email_service=EmailService(settings),
        audit_service=AuditService(db_session, settings),
        redis=None,
    )
    await service.state_store.store("st", OAuthState(code_verifier="v", redirect_uri="http://cb"))
    return await service.callback("entra_external", code="c", state="st", redirect_uri=None)


async def _links(db_session):
    rows = (await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider == ExternalProvider.ENTRA_EXTERNAL))).scalars().all()
    return [r.provider_user_id for r in rows]


async def test_portal_sign_in_links_by_the_id_token_oid_not_userinfo_sub(db_session, entra_provider_settings, rsa_keypair, monkeypatch):
    access, _, _ = await _sign_in(
        db_session, entra_provider_settings, monkeypatch, {"access_token": "at", "id_token": _id_token(rsa_keypair[0])}
    )
    assert access
    assert await _links(db_session) == [OID]

    # A second sign-in resolves to the same person through the oid link.
    users_before = (await db_session.execute(select(User))).scalars().all()
    await _sign_in(db_session, entra_provider_settings, monkeypatch, {"access_token": "at", "id_token": _id_token(rsa_keypair[0])})
    assert len((await db_session.execute(select(User))).scalars().all()) == len(users_before)
    assert await _links(db_session) == [OID]


@pytest.mark.parametrize(
    "case",
    ["missing", "wrong_key", "wrong_aud", "wrong_iss", "expired", "no_oid", "sub_mismatch", "alg_none", "garbage"],
)
async def test_tampered_or_unverifiable_id_token_is_refused(db_session, entra_provider_settings, rsa_keypair, monkeypatch, case):
    private_pem = rsa_keypair[0]
    tokens = {
        "missing": None,
        "wrong_key": _id_token(_other_private_pem()),
        "wrong_aud": _id_token(private_pem, aud="someone-else"),
        "wrong_iss": _id_token(private_pem, iss="https://evil.example/v2.0"),
        "expired": _id_token(private_pem, iat=int(time.time()) - 7200, exp=int(time.time()) - 3600),
        "no_oid": _id_token(private_pem, oid=None),
        "sub_mismatch": _id_token(private_pem, sub="a-different-pairwise-sub"),
        "alg_none": jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": PAIRWISE_SUB, "oid": OID, "exp": int(time.time()) + 300}, None, algorithm="none"),
        "garbage": "not.a.jwt",
    }
    token_data = {"access_token": "at"}
    if tokens[case] is not None:
        token_data["id_token"] = tokens[case]
    with pytest.raises(AuthError):
        await _sign_in(db_session, entra_provider_settings, monkeypatch, token_data)
    assert await _links(db_session) == []


# ---------------------------------------------------------------------------------------------------------
# CR-208 portal (BRIEF-008 Part 2): the REAL authorize route for entra_external uses ENTRA_REDIRECT_URI, and
# passes an allow-listed domain_hint (google) through to the provider; any other hint is refused.
# ---------------------------------------------------------------------------------------------------------
from urllib.parse import parse_qs, urlparse

from app.main import app as _app

PORTAL_CALLBACK = "https://devgenie.example/portal/callback"


@pytest.fixture()
async def authorize_client(client, rsa_keypair):
    settings = get_settings().model_copy(
        update={"ENTRA_TENANT_DOMAIN": "testtenant", "ENTRA_TENANT_ID": TENANT_ID, "ENTRA_CLIENT_ID": CLIENT_ID, "ENTRA_REDIRECT_URI": PORTAL_CALLBACK}
    )
    _app.dependency_overrides[get_settings] = lambda: settings
    yield client


async def test_entra_authorize_route_uses_the_configured_redirect(authorize_client):
    r = await authorize_client.get("/api/v1/oauth/entra_external/authorize")
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["authorization_url"]).query)
    assert q["redirect_uri"] == [PORTAL_CALLBACK]
    assert "domain_hint" not in q
    assert r.json()["state"]


async def test_entra_authorize_sends_entra_the_google_domain_hint(authorize_client):
    # The portal sends `google`; Entra External ID names its Google federation `google.com` and answers a bare
    # `google` with AADSTS90023 (BRIEF-018 item 2, the owner's first live sign-up).
    r = await authorize_client.get("/api/v1/oauth/entra_external/authorize", params={"domain_hint": "google"})
    assert r.status_code == 200, r.text
    assert parse_qs(urlparse(r.json()["authorization_url"]).query)["domain_hint"] == ["google.com"]


@pytest.mark.parametrize("hint", ["evil.example", "facebook", "", "google.com"])
async def test_entra_authorize_refuses_an_unlisted_domain_hint(authorize_client, hint):
    r = await authorize_client.get("/api/v1/oauth/entra_external/authorize", params={"domain_hint": hint})
    assert r.status_code == 422


# BRIEF-183 U4: the actual HTTP parsing and verified Entra claim mapping, never a mocked fetch_user_info.
from tests.test_oauth_verified_email import userinfo, welcome_offline  # noqa: F401
from app.services.oauth_providers import MicrosoftProvider


@pytest.mark.parametrize("claims", [
    {"given_name": "Grace", "family_name": "Hopper", "job_title": "Admiral"},
    {"givenName": "Grace", "surname": "Hopper", "jobTitle": "Admiral"},
])
async def test_u4_drop_prefill_mapping_real_microsoft_userinfo(userinfo, claims):
    userinfo.update({"sub": PAIRWISE_SUB, "email": EMAIL, "email_verified": True, **claims})
    provider = MicrosoftProvider(get_settings())
    info = await provider.fetch_user_info({"access_token": "offline"})
    assert (info.first_name, info.last_name, info.role) == ("Grace", "Hopper", "Admiral")
    assert provider.scopes == ["openid", "email", "profile"]
    assert provider.userinfo_endpoint == "https://graph.microsoft.com/oidc/userinfo"


@pytest.mark.parametrize("source", ["userinfo", "verified_claims"])
@pytest.mark.parametrize("camel", [False, True])
async def test_u4_drop_prefill_mapping_real_entra_verified_claims(userinfo, entra_provider_settings, rsa_keypair, source, camel):
    hints = {"givenName": "Grace", "surname": "Hopper", "jobTitle": "Admiral"} if camel else {"given_name": "Grace", "family_name": "Hopper", "job_title": "Admiral"}
    userinfo.update({"sub": PAIRWISE_SUB, "email": EMAIL, "email_verified": True, **(hints if source == "userinfo" else {})})
    provider = EntraExternalProvider(entra_provider_settings)
    info = await provider.fetch_user_info({"access_token": "offline", "id_token": _id_token(rsa_keypair[0], **(hints if source == "verified_claims" else {}))})
    assert info.sub == OID
    assert (info.first_name, info.last_name, info.role) == ("Grace", "Hopper", "Admiral")
    assert provider.scopes == ["openid", "email", "profile"]


async def test_u4_display_name_is_editable_first_name_without_surname_guess(userinfo):
    userinfo.update({"sub": "s", "email": EMAIL, "name": "Prince Rogers Nelson"})
    info = await MicrosoftProvider(get_settings()).fetch_user_info({})
    assert info.first_name == "Prince Rogers Nelson"
    assert info.last_name is None and info.role is None


@pytest.mark.parametrize("bad", [None, "", 42, {}, "X\nY", "x" * 121])
async def test_u4_invalid_or_missing_title_is_discarded(userinfo, bad):
    userinfo.update({"sub": "s", "email": EMAIL, "given_name": "Grace", "job_title": bad})
    info = await MicrosoftProvider(get_settings()).fetch_user_info({})
    assert info.first_name == "Grace"
    assert info.role is None


@pytest.mark.parametrize("verified", [True, False])
async def test_u4_real_entra_callback_and_pending_code_transport(db_session, fake_redis, sent_otps, monkeypatch, userinfo, entra_provider_settings, rsa_keypair, verified):
    userinfo.update({"sub": PAIRWISE_SUB, "email": EMAIL, "email_verified": verified})
    provider = EntraExternalProvider(entra_provider_settings)

    async def exchange(**kwargs):
        return {"access_token": "offline", "id_token": _id_token(rsa_keypair[0], given_name="Grace", family_name="Hopper", job_title="Admiral")}

    monkeypatch.setattr(provider, "exchange_code", exchange)
    registry = PluginRegistry()
    registry.register_oauth_provider(provider)
    settings = entra_provider_settings
    service = OAuthService(db_session, settings, registry, TokenService(db_session, settings), EmailService(settings), AuditService(db_session, settings), fake_redis)
    await service.state_store.store("prefill", OAuthState(code_verifier="v", redirect_uri="http://cb"))
    result = await service.callback("entra_external", "c", "prefill", None)
    if not verified:
        # Simulate a new request/service: only Redis, not an in-memory closure, transports the hints.
        pending_id = result.pending_id
        service = OAuthService(db_session, settings, registry, TokenService(db_session, settings), EmailService(settings), AuditService(db_session, settings), fake_redis)
        result = await service.verify(pending_id, sent_otps[-1])
    assert result[0]
    row = (await db_session.execute(select(User).where(User.normalized_email == EMAIL))).scalar_one()
    await db_session.refresh(row)
    assert (row.first_name, row.last_name, row.role, row.profile_completed) == ("Grace", "Hopper", "Admiral", False)
