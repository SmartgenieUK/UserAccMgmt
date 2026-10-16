"""CR-208 certification round 2, R2-F1: a successful OAuth sign-in durably saves the user, the provider link and
the refresh token.

Each request gets its own session, closed without committing, as production's get_session does; every
assertion reads back through a NEW session. Driven through the real routes with the real Entra provider
(ID token verified against a pinned test key); only the provider's HTTP client is faked.
"""
from __future__ import annotations

import json
import time

import jwt
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.main import app
from app.models import ExternalIdentity, Membership, Organization, RefreshToken, User
from app.models.enums import ExternalProvider, Role
from app.services import oauth_providers

TENANT_ID = "tenant-r2f1"
ISSUER = f"https://{TENANT_ID}.ciamlogin.com/{TENANT_ID}/v2.0"
CLIENT_ID = "client-r2f1"
SECRET = "svc-secret-0123456789-abcdefghij-R2F1"
OID = "7d2f6c1e-0000-4000-8000-00000000r2f1"
PAIRWISE = "pairwise-sub-r2f1"
EMAIL = "persist@example.com"


@pytest.fixture()
def entra(fresh_client, rsa_keypair, monkeypatch):
    """Configure the Entra provider + the by-subject secret; `entra.userinfo` sets what Graph userinfo answers."""
    private_pem, public_pem = rsa_keypair
    settings = get_settings().model_copy(
        update={
            "ENTRA_TENANT_DOMAIN": "testtenant",
            "ENTRA_TENANT_ID": TENANT_ID,
            "ENTRA_CLIENT_ID": CLIENT_ID,
            "ENTRA_CLIENT_SECRET": "x",
            "ENTRA_ISSUER": ISSUER,
            "ENTRA_SIGNING_PUBLIC_KEY_PEM": public_pem.decode(),
            "DEVGENIE_SERVICE_SECRET": SECRET,
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    now = int(time.time())
    id_token = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": PAIRWISE, "oid": OID, "iat": now, "exp": now + 300}, private_pem, algorithm="RS256"
    )
    userinfo: dict = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return dict(userinfo)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def fetch_token(self, *args, **kwargs):
            return {"access_token": "at", "id_token": id_token}

        async def get(self, url):
            return FakeResp()

        async def aclose(self):
            return None

    monkeypatch.setattr(oauth_providers, "AsyncOAuth2Client", FakeClient)
    return userinfo


async def _callback(client, fake_redis, state="st-r2f1"):
    fake_redis.store[f"oauth:state:{state}"] = json.dumps({"code_verifier": "v", "redirect_uri": "http://cb"})
    return await client.post("/api/v1/oauth/entra_external/callback", json={"code": "c", "state": state})


async def _assert_durable(fresh_client, session_factory, tokens, *, expect_user_id=None):
    async with session_factory() as s:
        user = (await s.execute(select(User).where(User.normalized_email == EMAIL))).scalar_one_or_none()
        assert user is not None, "user not persisted"
        if expect_user_id:
            assert user.id == expect_user_id
        link = (
            await s.execute(select(ExternalIdentity).where(ExternalIdentity.provider == ExternalProvider.ENTRA_EXTERNAL))
        ).scalar_one_or_none()
        assert link is not None, "provider link not persisted"
        assert link.provider_user_id == OID and link.user_id == user.id
        refresh_rows = (await s.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalars().all()
        assert refresh_rows, "refresh token not persisted"

    r = await fresh_client.post("/api/v1/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, r.text
    again = await fresh_client.post("/api/v1/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code != 200  # usable once: rotation revoked it

    r = await fresh_client.get(
        "/api/v1/internal/entitlements/by-subject",
        params={"provider": "entra", "oid": OID, "product": "devgenie"},
        headers={"X-Service-Secret": SECRET},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == str(user.id)


async def test_new_user_via_code_is_saved(fresh_client, session_factory, fake_redis, entra, sent_otps):
    entra.update({"sub": PAIRWISE, "email": EMAIL, "name": "New"})  # Graph userinfo: no email_verified
    r = await _callback(fresh_client, fake_redis)
    assert r.status_code == 202, r.text
    r = await fresh_client.post("/api/v1/oauth/verify", json={"pending_id": r.json()["pending_id"], "otp": sent_otps[-1]})
    assert r.status_code == 200, r.text
    await _assert_durable(fresh_client, session_factory, r.json())


async def test_existing_account_joined_via_code_is_saved(fresh_client, session_factory, fake_redis, entra, sent_otps):
    async with session_factory() as s:
        user = User(email=EMAIL, normalized_email=EMAIL, is_verified=True)
        org = Organization(name="Existing", slug="existing-r2f1")
        s.add_all([user, org])
        await s.flush()
        s.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))
        await s.commit()
        existing_id = user.id
    entra.update({"sub": PAIRWISE, "email": EMAIL, "name": "Existing"})
    r = await _callback(fresh_client, fake_redis)
    assert r.status_code == 202, r.text
    r = await fresh_client.post("/api/v1/oauth/verify", json={"pending_id": r.json()["pending_id"], "otp": sent_otps[-1]})
    assert r.status_code == 200, r.text
    await _assert_durable(fresh_client, session_factory, r.json(), expect_user_id=existing_id)


async def test_literal_true_direct_sign_in_is_saved(fresh_client, session_factory, fake_redis, entra, sent_otps):
    entra.update({"sub": PAIRWISE, "email": EMAIL, "email_verified": True, "name": "Direct"})
    r = await _callback(fresh_client, fake_redis)
    assert r.status_code == 200, r.text
    assert sent_otps == []
    await _assert_durable(fresh_client, session_factory, r.json())
