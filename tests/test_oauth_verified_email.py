"""CR-208 certification F1: no OAuth sign-in links or creates an account on an email the provider has not
verified (owner ruling 2026-10-16, verified email always).

Verified means the provider's userinfo says literal boolean `true`. Anything else (missing flag, the string
"false", boolean false) answers 202 verification_required and emails a one-time code; POST /oauth/verify
with that code completes the sign-in. An identity already linked signs in directly.

The reproduction drives the REAL provider parsing (BaseOAuthProvider.fetch_user_info) and the real callback
route; only the provider's HTTP client is faked.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Credential, ExternalIdentity, Membership, Organization, User
from app.models.enums import ExternalProvider, Role
from app.security.hashing import hash_password
from app.services import oauth_providers
from app.services.identity_link_service import IdentityLinkService
from app.services.oauth_providers import GoogleProvider
from app.core.config import get_settings
from app.core.exceptions import AuthError

VICTIM = "victim@example.com"
ATTACKER_SUB = "attacker-sub-1"
CALLBACK = "/api/v1/oauth/google/callback"
VERIFY = "/api/v1/oauth/verify"


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


@pytest.fixture()
def userinfo(monkeypatch):
    """Set what the provider's userinfo endpoint answers; everything else in the provider is real."""
    answer: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def fetch_token(self, *args, **kwargs):
            return {"access_token": "at"}

        async def get(self, url):
            return FakeResp(dict(answer))

        async def aclose(self):
            return None

    monkeypatch.setattr(oauth_providers, "AsyncOAuth2Client", FakeClient)
    return answer


async def _victim(db_session, *, verified=True, password=True):
    user = User(email=VICTIM, normalized_email=VICTIM, is_verified=verified)
    org = Organization(name="Victim Org", slug="victim-org")
    db_session.add_all([user, org])
    await db_session.flush()
    db_session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))
    if password:
        db_session.add(Credential(user_id=user.id, password_hash=hash_password("VictimPass1!")))
    await db_session.commit()
    return user


async def _callback(client, fake_redis, state="st-1"):
    fake_redis.store[f"oauth:state:{state}"] = json.dumps({"code_verifier": "v", "redirect_uri": "http://cb"})
    return await client.post(CALLBACK, json={"code": "c", "state": state})


async def _links(db_session, sub=ATTACKER_SUB):
    return (await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider_user_id == sub))).scalars().all()


# --- provider parsing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, verified",
    [
        ({"sub": "s", "email": VICTIM}, False),  # missing flag
        ({"sub": "s", "email": VICTIM, "email_verified": "false"}, False),
        ({"sub": "s", "email": VICTIM, "email_verified": "true"}, False),  # a string is not the boolean
        ({"sub": "s", "email": VICTIM, "email_verified": 1}, False),
        ({"sub": "s", "email": VICTIM, "email_verified": False}, False),
        ({"sub": "s", "email": VICTIM, "email_verified": True}, True),
    ],
)
async def test_email_verified_is_true_only_for_literal_boolean_true(userinfo, data, verified):
    userinfo.update(data)
    info = await GoogleProvider(get_settings()).fetch_user_info({})
    assert info.email_verified is verified


@pytest.mark.parametrize(
    "data",
    [{"sub": "s"}, {"sub": "s", "email": None}, {"sub": "s", "email": ""}, {"sub": "s", "userPrincipalName": "u@tenant.onmicrosoft.com"}],
)
async def test_a_missing_email_is_missing_never_the_string_none_or_the_upn(userinfo, data):
    userinfo.update(data)
    info = await GoogleProvider(get_settings()).fetch_user_info({})
    assert info.email is None


# --- the takeover reproduction ------------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [{}, {"email_verified": "false"}, {"email_verified": False}])
async def test_unverified_email_gets_a_code_not_the_victims_account(client, db_session, fake_redis, userinfo, sent_otps, flag):
    victim = await _victim(db_session)
    userinfo.update({"sub": ATTACKER_SUB, "email": VICTIM, "name": "Mallory", **flag})

    r = await _callback(client, fake_redis)

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "verification_required"
    assert body["pending_id"]
    assert body["email_hint"] == "v•••@example.com"
    assert "access_token" not in body and "refresh_token" not in body
    assert await _links(db_session) == []  # nothing linked
    await db_session.refresh(victim)
    cred = (await db_session.execute(select(Credential).where(Credential.user_id == victim.id))).scalar_one_or_none()
    assert cred is not None  # victim untouched
    assert len(sent_otps) == 1  # the code went to the address, not to the caller


async def test_unverified_email_for_an_unknown_address_creates_nobody(client, db_session, fake_redis, userinfo, sent_otps):
    userinfo.update({"sub": ATTACKER_SUB, "email": "nobody@example.com"})
    r = await _callback(client, fake_redis)
    assert r.status_code == 202
    assert (await db_session.execute(select(User))).scalars().all() == []


async def test_missing_email_is_refused(client, db_session, fake_redis, userinfo, sent_otps):
    userinfo.update({"sub": ATTACKER_SUB, "email_verified": True})
    r = await _callback(client, fake_redis)
    assert r.status_code == 401
    assert sent_otps == []


async def test_literal_true_signs_in_directly(client, db_session, fake_redis, userinfo, sent_otps):
    victim = await _victim(db_session)
    userinfo.update({"sub": "google-sub-real", "email": VICTIM, "email_verified": True})
    r = await _callback(client, fake_redis)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    links = await _links(db_session, "google-sub-real")
    assert [link.user_id for link in links] == [victim.id]
    assert sent_otps == []


async def test_already_linked_identity_signs_in_without_a_code(client, db_session, fake_redis, userinfo, sent_otps):
    victim = await _victim(db_session)
    db_session.add(ExternalIdentity(user_id=victim.id, provider=ExternalProvider.GOOGLE, provider_user_id="linked-sub", email=VICTIM))
    await db_session.commit()
    userinfo.update({"sub": "linked-sub", "email": VICTIM})  # no flag
    r = await _callback(client, fake_redis)
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    assert sent_otps == []


# --- POST /oauth/verify -------------------------------------------------------------------------------------


async def _pending(client, db_session, fake_redis, userinfo, sent_otps, *, victim=True):
    if victim:
        await _victim(db_session, verified=False)
    userinfo.update({"sub": ATTACKER_SUB, "email": VICTIM, "name": "Owner"})
    r = await _callback(client, fake_redis)
    assert r.status_code == 202, r.text
    return r.json()["pending_id"], sent_otps[-1]


def _wrong(otp: str) -> str:
    return "0" * len(otp) if otp != "0" * len(otp) else "1" * len(otp)


async def test_correct_code_joins_the_existing_account_and_issues_tokens(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] and r.json()["refresh_token"]
    user = (await db_session.execute(select(User).where(User.normalized_email == VICTIM))).scalar_one()
    await db_session.refresh(user)
    assert user.is_verified is True
    assert [link.user_id for link in await _links(db_session)] == [user.id]


async def test_correct_code_for_a_new_address_creates_a_verified_user(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps, victim=False)
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})
    assert r.status_code == 200, r.text
    user = (await db_session.execute(select(User).where(User.normalized_email == VICTIM))).scalar_one()
    assert user.is_verified is True


async def test_wrong_code_is_refused_and_links_nothing(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": _wrong(otp)})
    assert r.status_code == 401
    assert "access_token" not in r.text
    assert await _links(db_session) == []


async def test_the_pending_id_alone_grants_nothing(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, _ = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    for body in ({"pending_id": pending_id, "otp": ""}, {"pending_id": pending_id}):
        r = await client.post(VERIFY, json=body)
        assert r.status_code in (401, 422)
    assert await _links(db_session) == []


async def test_expired_pending_sign_in_is_refused(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    for key in [k for k in fake_redis.store if pending_id in k]:
        del fake_redis.store[key]  # what the 10-minute TTL does
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})
    assert r.status_code == 401
    assert await _links(db_session) == []


async def test_a_pending_id_is_single_use(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    assert (await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})).status_code == 200
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})
    assert r.status_code == 401


async def test_the_sixth_attempt_is_refused_even_with_the_right_code(client, db_session, fake_redis, userinfo, sent_otps):
    pending_id, otp = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    for _ in range(5):
        assert (await client.post(VERIFY, json={"pending_id": pending_id, "otp": _wrong(otp)})).status_code == 401
    r = await client.post(VERIFY, json={"pending_id": pending_id, "otp": otp})
    assert r.status_code == 401
    assert await _links(db_session) == []


async def test_the_pending_store_carries_a_ten_minute_expiry(client, db_session, fake_redis, userinfo, sent_otps, monkeypatch):
    ttls: dict[str, int] = {}
    real_setex = fake_redis.setex

    async def setex(key, ttl, value):
        ttls[key] = ttl
        await real_setex(key, ttl, value)

    monkeypatch.setattr(fake_redis, "setex", setex)
    pending_id, _ = await _pending(client, db_session, fake_redis, userinfo, sent_otps)
    assert [ttl for key, ttl in ttls.items() if pending_id in key] == [600]


# --- belt and braces in the link service --------------------------------------------------------------------


async def test_link_service_refuses_to_attach_to_an_existing_account_without_a_verified_email(db_session):
    victim = await _victim(db_session)
    link = IdentityLinkService(db_session)
    with pytest.raises(AuthError):
        await link.find_or_create_user(ExternalProvider.GOOGLE, ATTACKER_SUB, VICTIM, "Mallory", email_verified=False)
    assert await _links(db_session) == []
    user = await link.find_or_create_user(ExternalProvider.GOOGLE, ATTACKER_SUB, VICTIM, "Owner", email_verified=True)
    assert user.id == victim.id
