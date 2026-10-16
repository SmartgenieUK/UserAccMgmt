"""CR-208 work item C: the service-to-service entitlement lookup DevGenie calls, keyed on the Entra oid.

GET /api/v1/internal/entitlements/by-subject?provider=entra&oid=<oid>&product=devgenie, authenticated by the
X-Service-Secret header against DEVGENIE_SERVICE_SECRET. Unset secret -> 404 to everyone; wrong/missing secret
-> 401 with no detail; unknown oid -> 404; never creates a user.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import app
from app.models import ExternalIdentity, Membership, Organization, Plan, Subscription, User
from app.models.enums import ExternalProvider, Role
from app.utils.time import utcnow

ENDPOINT = "/api/v1/internal/entitlements/by-subject"
SECRET = "svc-secret-0123456789-abcdefghij-XYZ"
OID = "7d2f6c1e-0000-4000-8000-00000000aaaa"
PRODUCT = "devgenie"


def _settings(secret):
    return get_settings().model_copy(update={"DEVGENIE_SERVICE_SECRET": secret})


async def _client(db_session, fake_redis, secret):
    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: _settings(secret)
    app.state.redis = fake_redis
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
async def svc(db_session, fake_redis):
    async with await _client(db_session, fake_redis, SECRET) as c:
        yield c
    app.dependency_overrides.clear()
    app.state.redis = None


@pytest.fixture()
async def svc_unconfigured(db_session, fake_redis):
    async with await _client(db_session, fake_redis, None) as c:
        yield c
    app.dependency_overrides.clear()
    app.state.redis = None


async def _linked_user(db_session, *, oid=OID, email="ada@example.com"):
    """A user linked to an Entra oid, in two orgs; the earlier membership is the primary org."""
    user = User(email=email, normalized_email=email, is_verified=True)
    primary = Organization(name="Primary", slug=f"primary-{oid[-4:]}")
    other = Organization(name="Other", slug=f"other-{oid[-4:]}")
    db_session.add_all([user, primary, other])
    await db_session.flush()
    db_session.add(Membership(user_id=user.id, org_id=primary.id, role=Role.ADMIN, created_at=utcnow() - timedelta(days=2)))
    db_session.add(Membership(user_id=user.id, org_id=other.id, role=Role.ADMIN, created_at=utcnow() - timedelta(days=1)))
    db_session.add(ExternalIdentity(user_id=user.id, provider=ExternalProvider.ENTRA_EXTERNAL, provider_user_id=oid, email=email))
    await db_session.commit()
    return user, primary, other


async def _subscribe(db_session, org, *, tier="pro", status="active", period_end=None):
    plan = (await db_session.execute(select(Plan).where(Plan.product == PRODUCT, Plan.code == tier))).scalar_one()
    db_session.add(
        Subscription(org_id=org.id, product=PRODUCT, plan_id=plan.id, status=status, source="manual", current_period_end=period_end)
    )
    await db_session.commit()


def _params(oid=OID, provider="entra", product=PRODUCT):
    return {"provider": provider, "oid": oid, "product": product}


async def test_active_pro_subscription_resolves_for_the_linked_person(svc, db_session):
    user, primary, other = await _linked_user(db_session)
    end = utcnow() + timedelta(days=30)
    await _subscribe(db_session, primary, period_end=end)
    await _subscribe(db_session, other, tier="enterprise")  # not the primary org: must not leak in

    r = await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"tier", "status", "expires_at", "org_id", "account_id"}
    assert body["tier"] == "pro"
    assert body["status"] == "active"
    assert body["org_id"] == str(primary.id)
    assert body["account_id"] == str(user.id)  # the person, not the org
    assert body["expires_at"] is not None


async def test_trialing_counts_as_entitled(svc, db_session):
    _, primary, _ = await _linked_user(db_session)
    await _subscribe(db_session, primary, status="trialing")
    body = (await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})).json()
    assert body["tier"] == "pro" and body["status"] == "trialing"


@pytest.mark.parametrize("status", ["cancelled", "past_due"])
async def test_lapsed_subscription_returns_what_resolve_returns(svc, db_session, status):
    _, primary, _ = await _linked_user(db_session)
    await _subscribe(db_session, primary, status=status)
    r = await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "lite" and body["status"] == status and body["expires_at"] is None


async def test_no_subscription_is_lite(svc, db_session):
    await _linked_user(db_session)
    body = (await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})).json()
    assert body["tier"] == "lite" and body["status"] == "none"


async def test_unknown_oid_is_404_and_creates_no_user(svc, db_session):
    await _linked_user(db_session)
    before = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    r = await svc.get(ENDPOINT, params=_params(oid="not-a-linked-oid"), headers={"X-Service-Secret": SECRET})
    assert r.status_code == 404
    after = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    assert after == before


async def test_oid_linked_under_another_provider_is_404(svc, db_session):
    user = User(email="g@example.com", normalized_email="g@example.com", is_verified=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(ExternalIdentity(user_id=user.id, provider=ExternalProvider.GOOGLE, provider_user_id=OID))
    await db_session.commit()
    r = await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})
    assert r.status_code == 404


async def test_unknown_provider_is_404(svc, db_session):
    await _linked_user(db_session)
    r = await svc.get(ENDPOINT, params=_params(provider="google"), headers={"X-Service-Secret": SECRET})
    assert r.status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Service-Secret": "wrong"}, {"X-Service-Secret": SECRET + "x"}, {"X-Service-Secret": ""}])
async def test_wrong_or_missing_secret_is_401_with_no_detail(svc, db_session, headers):
    await _linked_user(db_session)
    r = await svc.get(ENDPOINT, params=_params(), headers=headers)
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "auth_failed", "message": "Authentication failed"}}


async def test_secret_unset_is_404_to_everyone(svc_unconfigured, db_session):
    await _linked_user(db_session)
    for headers in ({}, {"X-Service-Secret": SECRET}, {"X-Service-Secret": ""}):
        r = await svc_unconfigured.get(ENDPOINT, params=_params(), headers=headers)
        assert r.status_code == 404


async def test_the_secret_never_appears_in_a_response_or_log_line(svc, db_session, caplog):
    _, primary, _ = await _linked_user(db_session)
    await _subscribe(db_session, primary)
    caplog.set_level(logging.DEBUG)
    responses = [
        await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET}),
        await svc.get(ENDPOINT, params=_params(oid="nope"), headers={"X-Service-Secret": SECRET}),
        await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET[:-1]}),
    ]
    for r in responses:
        assert SECRET not in r.text
        assert SECRET[:-1] not in r.text
        assert all(SECRET[:-1] not in v for v in r.headers.values())
    assert SECRET[:-1] not in caplog.text


async def test_deactivated_user_is_404_even_with_a_paid_subscription(svc, db_session):
    user, primary, _ = await _linked_user(db_session)
    await _subscribe(db_session, primary)
    user.is_active = False
    await db_session.commit()
    r = await svc.get(ENDPOINT, params=_params(), headers={"X-Service-Secret": SECRET})
    assert r.status_code == 404
