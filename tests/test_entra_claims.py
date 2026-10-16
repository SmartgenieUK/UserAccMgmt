from __future__ import annotations

import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.models import Credential, ExternalIdentity, Membership, Organization, Plan, Subscription, User
from app.models.enums import ExternalProvider, Role
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


async def _linked(db_session, oid: str, email: str, *, tier: str | None = None):
    """A UAM user already linked to this Entra user id, with a personal org (and optionally a subscription)."""
    user = User(email=email, normalized_email=email, is_verified=True)
    org = Organization(name=f"{email} org", slug=f"org-{oid}")
    db_session.add_all([user, org])
    await db_session.flush()
    db_session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))
    db_session.add(ExternalIdentity(user_id=user.id, provider=ExternalProvider.ENTRA_EXTERNAL, provider_user_id=oid, email=email))
    if tier:
        plan = (await db_session.execute(select(Plan).where(Plan.product == PRODUCT, Plan.code == tier))).scalar_one()
        db_session.add(Subscription(org_id=org.id, product=PRODUCT, plan_id=plan.id, status="active", source="manual"))
    await db_session.commit()
    return user, org


async def _call(entra_client, rsa_keypair, oid, mail, **kw):
    return await entra_client.post(
        ENDPOINT, headers={"Authorization": f"Bearer {_token(rsa_keypair)}"}, json=_callout(oid, mail=mail, **kw)
    )


async def test_linked_user_gets_their_claims_as_strings(entra_client, db_session, rsa_keypair):
    user, org = await _linked(db_session, "entra-sub-1", "ada@customer.example")
    res = await _call(entra_client, rsa_keypair, "entra-sub-1", "ada@customer.example")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"]["@odata.type"] == "microsoft.graph.onTokenIssuanceStartResponseData"
    claims = _claims(body)
    assert claims["tier"] == "lite"
    assert claims["org_id"] == str(org.id)
    assert claims["scope"] == []
    # All claim values are strings or string arrays (Entra contract).
    for value in claims.values():
        assert isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))


async def test_repeated_callout_for_a_linked_user_writes_nothing(entra_client, db_session, rsa_keypair):
    await _linked(db_session, "entra-sub-2", "grace@customer.example")
    for _ in range(2):
        res = await _call(entra_client, rsa_keypair, "entra-sub-2", "grace@customer.example")
        assert res.status_code == 200, res.text
    assert len((await db_session.execute(select(User))).scalars().all()) == 1
    assert len((await db_session.execute(select(ExternalIdentity))).scalars().all()) == 1


async def test_active_subscription_tier_is_surfaced(entra_client, db_session, rsa_keypair):
    await _linked(db_session, "entra-sub-3", "edsger@customer.example", tier="pro")
    res = await _call(entra_client, rsa_keypair, "entra-sub-3", "edsger@customer.example")
    assert _claims(res.json())["tier"] == "pro"


async def test_unlinked_user_gets_empty_claims_and_nothing_is_created(entra_client, db_session, rsa_keypair):
    res = await _call(entra_client, rsa_keypair, "entra-sub-new", "newcomer@customer.example")
    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}
    assert (await db_session.execute(select(User))).scalars().all() == []
    assert (await db_session.execute(select(ExternalIdentity))).scalars().all() == []


async def test_unlinked_user_matching_a_paid_account_by_email_gets_nothing(entra_client, db_session, rsa_keypair):
    """CR-208 certification F1, amendment A1: the callout never joins an account by email."""
    victim, _ = await _linked(db_session, "entra-sub-victim", "victim@customer.example", tier="pro")
    db_session.add(Credential(user_id=victim.id, password_hash=hash_password("VictimPass1!")))
    await db_session.commit()

    res = await _call(entra_client, rsa_keypair, "entra-sub-attacker", "victim@customer.example")

    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}
    idents = (await db_session.execute(select(ExternalIdentity))).scalars().all()
    assert [i.provider_user_id for i in idents] == ["entra-sub-victim"]  # no identity attached to the victim
    assert len((await db_session.execute(select(User))).scalars().all()) == 1
    cred = (await db_session.execute(select(Credential).where(Credential.user_id == victim.id))).scalar_one_or_none()
    assert cred is not None  # victim untouched


async def test_unlinked_user_matching_an_unverified_account_claims_nothing(entra_client, db_session, rsa_keypair):
    squatter = User(email="sq@customer.example", normalized_email="sq@customer.example", is_verified=False)
    db_session.add(squatter)
    await db_session.flush()
    db_session.add(Credential(user_id=squatter.id, password_hash=hash_password("SquatterPass1!")))
    await db_session.commit()

    res = await _call(entra_client, rsa_keypair, "entra-sub-sq", "sq@customer.example")

    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}
    await db_session.refresh(squatter)
    assert squatter.is_verified is False
    assert (await db_session.execute(select(ExternalIdentity))).scalars().all() == []


async def test_deactivated_linked_user_gets_empty_claims(entra_client, db_session, rsa_keypair):
    user, _ = await _linked(db_session, "entra-sub-off", "off@customer.example", tier="pro")
    user.is_active = False
    await db_session.commit()
    res = await _call(entra_client, rsa_keypair, "entra-sub-off", "off@customer.example")
    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}


async def test_unmapped_client_app_returns_empty_claims(entra_client, db_session, rsa_keypair):
    await _linked(db_session, "entra-sub-4", "alan@customer.example", tier="pro")
    res = await _call(entra_client, rsa_keypair, "entra-sub-4", "alan@customer.example", app_id="some-unknown-app")
    assert res.status_code == 200, res.text
    assert _claims(res.json()) == {}


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
