from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Membership, Plan, Role, Subscription
from app.utils.time import utcnow

PASSWORD = "StrongPass1!"
PRODUCT = "devgenie"


async def _signup(client, sent_otps, email: str) -> dict:
    """Register + verify + login; returns {"headers", "user_id", "org_id"}."""
    before = len(sent_otps)
    assert (await client.post("/api/v1/register", json={"email": email, "password": PASSWORD})).status_code == 201
    assert (await client.post("/api/v1/verify-email", json={"email": email, "otp": sent_otps[before]})).status_code == 200
    login = await client.post("/api/v1/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    orgs = await client.get("/api/v1/orgs", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    return {
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
        "user_id": me.json()["id"],
        "org_id": orgs.json()[0]["id"],
    }


@pytest.fixture()
async def admin(client, sent_otps):
    return await _signup(client, sent_otps, "admin@example.com")


async def _add_member(client, db_session, sent_otps, org_id: str, email: str) -> dict:
    """A second user who is a MEMBER of the admin's org; token scoped to that org."""
    user = await _signup(client, sent_otps, email)
    db_session.add(Membership(user_id=user["user_id"], org_id=org_id, role=Role.MEMBER))
    await db_session.commit()
    login = await client.post("/api/v1/login", json={"email": email, "password": PASSWORD, "org_id": org_id})
    assert login.status_code == 200, login.text
    user["headers"] = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return user


async def test_no_subscription_resolves_to_lite(client, admin):
    res = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=admin["headers"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tier"] == "lite" and body["status"] == "none" and body["source"] is None
    assert body["org_id"] == admin["org_id"] and body["account_id"] == admin["org_id"]
    assert body["seats_used"] == 0


async def test_unknown_product_is_rejected(client, admin):
    res = await client.get("/api/v1/entitlements/me", params={"product": "nope"}, headers=admin["headers"])
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "unknown_plan"


async def test_admin_override_sets_tier_and_is_audited(client, db_session, admin):
    url = f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}"
    res = await client.put(url, json={"tier": "pro", "seats": 2}, headers=admin["headers"])
    assert res.status_code == 200, res.text
    assert res.json()["tier"] == "pro" and res.json()["seats"] == 2 and res.json()["source"] == "manual"

    me = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=admin["headers"])
    assert me.json()["tier"] == "pro" and me.json()["status"] == "active"

    # Re-override replaces, never duplicates (unique org × product).
    again = await client.put(url, json={"tier": "enterprise"}, headers=admin["headers"])
    assert again.json()["tier"] == "enterprise" and again.json()["seats"] is None
    subs = (await db_session.execute(select(Subscription).where(Subscription.org_id == admin["org_id"]))).scalars().all()
    assert len(subs) == 1

    listing = await client.get(f"/api/v1/orgs/{admin['org_id']}/entitlements", headers=admin["headers"])
    assert {e["product"]: e["tier"] for e in listing.json()} == {"cloudgenie": "lite", "devgenie": "enterprise"}


async def test_member_cannot_override(client, db_session, sent_otps, admin):
    member = await _add_member(client, db_session, sent_otps, admin["org_id"], "member@example.com")
    res = await client.put(
        f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}", json={"tier": "pro"}, headers=member["headers"]
    )
    assert res.status_code == 403
    # ...but can read the org's entitlement.
    read = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=member["headers"])
    assert read.status_code == 200 and read.json()["org_id"] == admin["org_id"]


async def test_admin_of_another_org_cannot_override(client, sent_otps, admin):
    other = await _signup(client, sent_otps, "other@example.com")
    res = await client.put(
        f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}", json={"tier": "pro"}, headers=other["headers"]
    )
    assert res.status_code == 403


async def test_seat_cap_is_enforced(client, db_session, sent_otps, admin):
    member = await _add_member(client, db_session, sent_otps, admin["org_id"], "member@example.com")
    base = f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}"
    assert (await client.put(base, json={"tier": "pro", "seats": 1}, headers=admin["headers"])).status_code == 200

    first = await client.post(f"{base}/seats", json={"user_id": admin["user_id"]}, headers=admin["headers"])
    assert first.status_code == 200 and first.json()["seats_used"] == 1
    # Idempotent: assigning the same seat again does not consume another.
    assert (await client.post(f"{base}/seats", json={"user_id": admin["user_id"]}, headers=admin["headers"])).json()["seats_used"] == 1

    second = await client.post(f"{base}/seats", json={"user_id": member["user_id"]}, headers=admin["headers"])
    assert second.status_code == 409 and second.json()["error"]["code"] == "seat_cap"

    freed = await client.delete(f"{base}/seats/{admin['user_id']}", headers=admin["headers"])
    assert freed.json()["seats_used"] == 0
    assert (await client.post(f"{base}/seats", json={"user_id": member["user_id"]}, headers=admin["headers"])).status_code == 200

    stranger = await client.post(f"{base}/seats", json={"user_id": "00000000-0000-0000-0000-000000000000"}, headers=admin["headers"])
    assert stranger.status_code == 404


@pytest.mark.parametrize("status,period_end", [("past_due", None), ("cancelled", None), ("active", -1)])
async def test_lapsed_subscription_falls_back_to_lite(client, db_session, admin, status, period_end):
    base = f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}"
    assert (await client.put(base, json={"tier": "pro"}, headers=admin["headers"])).status_code == 200

    sub = (await db_session.execute(select(Subscription).where(Subscription.org_id == admin["org_id"]))).scalar_one()
    sub.status = status
    if period_end is not None:
        sub.current_period_end = utcnow() + timedelta(days=period_end)
    await db_session.commit()

    me = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=admin["headers"])
    assert me.json()["tier"] == "lite" and me.json()["status"] == status


async def test_deactivated_plan_falls_back_to_lite(client, db_session, admin):
    """A withdrawn plan (is_active=False) must not keep serving its paid tier (review finding)."""
    base = f"/api/v1/orgs/{admin['org_id']}/entitlements/{PRODUCT}"
    assert (await client.put(base, json={"tier": "pro"}, headers=admin["headers"])).status_code == 200

    sub = (await db_session.execute(select(Subscription).where(Subscription.org_id == admin["org_id"]))).scalar_one()
    plan = await db_session.get(Plan, sub.plan_id)
    plan.is_active = False
    await db_session.commit()

    me = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=admin["headers"])
    assert me.json()["tier"] == "lite" and me.json()["status"] == "active"
