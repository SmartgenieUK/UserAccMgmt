from __future__ import annotations

import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.models import Credential, ExternalIdentity, Membership, Plan, Subscription, User
from app.models.enums import ExternalProvider
from app.security.entra_jwt import ENTRA_AUTH_EVENTS_APP_ID
from app.security.hashing import hash_password

TENANT_ID = "e11f2537-237e-48cd-b3f1-54b0ff7e5835"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
EXTENSION_APP_ID = "extension-app-registration-id"  # aud of the inbound callout token
DEVGENIE_CLIENT_APP_ID = "client-app-devgenie"
PRODUCT = "devgenie"
ENDPOINT = "/api/v1/entra/token-issuance"


@pytest.fixture()
def entra_settings(rsa_keypair):
    _, public_pem = rsa_keypair
    base = get_settings()
    return base.model_copy(
        update={
            "ENTRA_TENANT_ID": TENANT_ID,
            "ENTRA_ISSUER": ISSUER,
            "ENTRA_EXTENSION_APP_ID": EXTENSION_APP_ID,
            "ENTRA_SIGNING_PUBLIC_KEY_PEM": public_pem.decode(),
            "ENTRA_PRODUCT_APP_IDS": {DEVGENIE_CLIENT_APP_ID: PRODUCT},
        }
    )


@pytest.fixture()
async def entra_client(db_session, fake_redis, entra_settings):
    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: entra_settings
    app.state.redis = fake_redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.redis = None


def _token(rsa_keypair, *, azp=ENTRA_AUTH_EVENTS_APP_ID, aud=EXTENSION_APP_ID, iss=ISSUER, exp_delta=300):
    private_pem, _ = rsa_keypair
    now = int(time.time())
    return jwt.encode(
        {"iss": iss, "aud": aud, "azp": azp, "iat": now, "exp": now + exp_delta},
        private_pem,
        algorithm="RS256",
        headers={"kid": "entra-signing-1"},
    )


def _callout(user_id: str, *, mail: str, app_id: str = DEVGENIE_CLIENT_APP_ID, name: str = "Ada Lovelace") -> dict:
    return {
        "type": "microsoft.graph.authenticationEvent.tokenIssuanceStart",
        "data": {
            "@odata.type": "microsoft.graph.onTokenIssuanceStartCalloutData",
            "tenantId": TENANT_ID,
            "authenticationContext": {
                "correlationId": "00000000-0000-0000-0000-0000000000cc",
                "clientServicePrincipal": {"id": "sp-id", "appId": app_id, "displayName": "DevGenie"},
                "user": {"id": user_id, "mail": mail, "displayName": name, "userType": "Member"},
            },
        },
    }


def _claims(body: dict) -> dict:
    actions = body["data"]["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["@odata.type"] == "microsoft.graph.tokenIssuanceStart.provideClaimsForToken"
    return action["claims"]


async def test_valid_callout_jit_creates_user_and_returns_lite_claims(entra_client, db_session, rsa_keypair):
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
        json=_callout("entra-sub-1", mail="ada@customer.example"),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"]["@odata.type"] == "microsoft.graph.onTokenIssuanceStartResponseData"
    claims = _claims(body)
    assert claims["tier"] == "lite"
    assert claims["org_id"] and claims["account_id"] == claims["org_id"]
    assert claims["scope"] == []
    # All claim values are strings or string arrays (Entra contract).
    for value in claims.values():
        assert isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))

    user = (await db_session.execute(select(User).where(User.normalized_email == "ada@customer.example"))).scalar_one()
    identity = (await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id))).scalar_one()
    assert identity.provider == ExternalProvider.ENTRA_EXTERNAL and identity.provider_user_id == "entra-sub-1"


async def test_repeated_callout_relinks_same_user(entra_client, db_session, rsa_keypair):
    for _ in range(2):
        res = await entra_client.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
            json=_callout("entra-sub-2", mail="grace@customer.example"),
        )
        assert res.status_code == 200, res.text
    users = (await db_session.execute(select(User).where(User.normalized_email == "grace@customer.example"))).scalars().all()
    assert len(users) == 1
    identities = (await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider_user_id == "entra-sub-2"))).scalars().all()
    assert len(identities) == 1


async def test_active_subscription_tier_is_surfaced(entra_client, db_session, rsa_keypair):
    # First call creates the user + personal org.
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
        json=_callout("entra-sub-3", mail="edsger@customer.example"),
    )
    org_id = _claims(res.json())["org_id"]
    pro = (await db_session.execute(select(Plan).where(Plan.product == PRODUCT, Plan.code == "pro"))).scalar_one()
    db_session.add(Subscription(org_id=org_id, product=PRODUCT, plan_id=pro.id, status="active", source="manual"))
    await db_session.commit()

    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
        json=_callout("entra-sub-3", mail="edsger@customer.example"),
    )
    assert _claims(res.json())["tier"] == "pro"


async def test_unmapped_client_app_returns_empty_claims(entra_client, rsa_keypair):
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
        json=_callout("entra-sub-4", mail="alan@customer.example", app_id="some-unknown-app"),
    )
    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}


async def test_unverified_squatter_account_is_claimed_and_credential_dropped(entra_client, db_session, rsa_keypair):
    """A verified Entra email is authoritative over an UNVERIFIED local account: the callout claims it and
    drops the squatter's untrusted credential, so a pre-registration attacker's password cannot ride the
    victim's federated identity (review finding, CRITICAL account-takeover)."""
    squatter = User(email="victim@customer.example", normalized_email="victim@customer.example", is_verified=False)
    db_session.add(squatter)
    await db_session.flush()
    db_session.add(Credential(user_id=squatter.id, password_hash=hash_password("SquatterPass1!")))
    await db_session.commit()

    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair)}"},
        json=_callout("entra-sub-victim", mail="victim@customer.example"),
    )
    assert res.status_code == 200, res.text

    users = (await db_session.execute(select(User).where(User.normalized_email == "victim@customer.example"))).scalars().all()
    assert len(users) == 1
    user = users[0]
    assert user.is_verified is True
    cred = (await db_session.execute(select(Credential).where(Credential.user_id == user.id))).scalar_one_or_none()
    assert cred is None  # the untrusted squatter credential is gone — the attacker's password no longer logs in
    ident = (await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.provider_user_id == "entra-sub-victim"))).scalar_one()
    assert ident.user_id == user.id


async def test_wrong_azp_is_rejected(entra_client, rsa_keypair):
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair, azp='not-the-auth-events-app')}"},
        json=_callout("entra-sub-5", mail="x@customer.example"),
    )
    assert res.status_code == 401


async def test_wrong_audience_is_rejected(entra_client, rsa_keypair):
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair, aud='some-other-resource')}"},
        json=_callout("entra-sub-6", mail="y@customer.example"),
    )
    assert res.status_code == 401


async def test_expired_token_is_rejected(entra_client, rsa_keypair):
    res = await entra_client.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {_token(rsa_keypair, exp_delta=-10)}"},
        json=_callout("entra-sub-7", mail="z@customer.example"),
    )
    assert res.status_code == 401


async def test_missing_bearer_is_rejected(entra_client, rsa_keypair):
    res = await entra_client.post(ENDPOINT, json=_callout("entra-sub-8", mail="w@customer.example"))
    assert res.status_code == 401
