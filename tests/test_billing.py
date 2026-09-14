from __future__ import annotations

import time
import hmac
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select
from fastapi import Request

from app.core.config import get_settings
from app.models import AuditEvent, Membership, Plan, Role, Subscription
from app.services.stripe_gateway import StripeGateway, get_stripe_gateway
from app.main import app

PASSWORD = "StrongPass1!"

async def _signup(client, sent_otps, email: str) -> dict:
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
    user = await _signup(client, sent_otps, email)
    db_session.add(Membership(user_id=user["user_id"], org_id=org_id, role=Role.MEMBER))
    await db_session.commit()
    login = await client.post("/api/v1/login", json={"email": email, "password": PASSWORD, "org_id": org_id})
    assert login.status_code == 200, login.text
    user["headers"] = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return user


class FakeStripeGateway(StripeGateway):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []

    def create_customer(self, name: str, metadata: dict) -> str:
        self.calls.append(("create_customer", name, metadata))
        return "cus_test123"

    def create_checkout_session(self, customer_id: str, price_id: str, success_url: str, cancel_url: str, metadata: dict) -> str:
        self.calls.append(("create_checkout_session", customer_id, price_id, metadata))
        return "https://checkout.stripe.test/session"

    def create_portal_session(self, customer_id: str, return_url: str) -> str:
        self.calls.append(("create_portal_session", customer_id))
        return "https://portal.stripe.test/session"

    def construct_event(self, payload: bytes, sig_header: str) -> dict:
        import stripe
        return stripe.Webhook.construct_event(payload, sig_header, self.settings.STRIPE_WEBHOOK_SECRET).to_dict()


@pytest.fixture
def billing_settings():
    def get_test_settings():
        s = get_settings()
        s.STRIPE_WEBHOOK_SECRET = "whsec_test"
        s.STRIPE_PRICE_MAP = {"price_pro": "devgenie:pro", "price_ent": "devgenie:enterprise"}
        s.STRIPE_CHECKOUT_SUCCESS_URL = "http://test/success"
        s.STRIPE_CHECKOUT_CANCEL_URL = "http://test/cancel"
        s.STRIPE_PORTAL_RETURN_URL = "http://test/portal"
        return s

    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_stripe_gateway] = lambda: FakeStripeGateway(get_test_settings())
    yield
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_stripe_gateway, None)


def sign(payload_dict, secret="whsec_test"):
    payload = json.dumps(payload_dict)
    ts = int(time.time())
    signed = f"{ts}.{payload}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return payload, f"t={ts},v1={sig}"

@pytest.mark.asyncio
async def test_checkout_session_requires_admin(client, db_session, sent_otps, admin, billing_settings):
    member = await _add_member(client, db_session, sent_otps, admin["org_id"], "member@example.com")
    
    # member gets 403
    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=member["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 403

    admin_headers = admin["headers"]
    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=admin_headers, json={"price_id": "price_pro"})
    assert res.status_code == 200
    assert res.json() == {"url": "https://checkout.stripe.test/session"}

    from app.models import Organization
    org = (await db_session.execute(select(Organization).where(Organization.id == uuid.UUID(admin["org_id"])))).scalar_one()
    assert org.stripe_customer_id == "cus_test123"

@pytest.mark.asyncio
async def test_checkout_unknown_price_rejected(client, db_session, admin, billing_settings):
    admin_headers = admin["headers"]

    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=admin_headers, json={"price_id": "price_unknown"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "unknown_price"

@pytest.mark.asyncio
async def test_checkout_other_org_forbidden(client, sent_otps, admin, billing_settings):
    other_admin = await _signup(client, sent_otps, "other@example.com")
    
    other_admin_headers = other_admin["headers"]

    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=other_admin_headers, json={"price_id": "price_pro"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_portal_session_requires_customer(client, db_session, admin, billing_settings):
    admin_headers = admin["headers"]

    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/portal-session", headers=admin_headers)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "no_customer"

    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=admin_headers, json={"price_id": "price_pro"})
    assert res.status_code == 200

    res = await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/portal-session", headers=admin_headers)
    assert res.status_code == 200
    assert res.json() == {"url": "https://portal.stripe.test/session"}

@pytest.mark.asyncio
async def test_webhook_bad_signature_rejected(client, db_session, billing_settings):
    payload, _ = sign({"id": "evt_1", "type": "checkout.session.completed"}, "wrong_secret")
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": "t=1,v1=wrong", "content-type": "application/json"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_signature"

    subs = (await db_session.execute(select(Subscription))).scalars().all()
    assert len(subs) == 0

@pytest.mark.asyncio
async def test_webhook_checkout_completed_activates_subscription(client, db_session, admin, billing_settings):
    event_dict = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_test123",
                "subscription": "sub_test1",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro"
                }
            }
        }
    }
    payload, sig = sign(event_dict)
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res.status_code == 200
    assert res.json() == {"received": True}

    admin_headers = admin["headers"]

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "pro"
    assert data["status"] == "active"
    assert data["source"] == "stripe"

@pytest.mark.asyncio
async def test_webhook_is_idempotent(client, db_session, admin, billing_settings):
    event_dict = {
        "id": "evt_2",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_test123",
                "subscription": "sub_test1",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro"
                }
            }
        }
    }
    payload, sig = sign(event_dict)
    res1 = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res1.status_code == 200

    res2 = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res2.status_code == 200

    subs = (await db_session.execute(select(Subscription).where(Subscription.org_id == uuid.UUID(admin["org_id"]), Subscription.product == "devgenie"))).scalars().all()
    assert len(subs) == 1

@pytest.mark.asyncio
async def test_webhook_subscription_deleted_falls_back_to_lite(client, db_session, admin, billing_settings):
    # first complete checkout
    event_dict = {
        "id": "evt_3",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_test123",
                "subscription": "sub_test2",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro"
                }
            }
        }
    }
    payload, sig = sign(event_dict)
    await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})

    # then delete
    del_event_dict = {
        "id": "evt_4",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_test2"
            }
        }
    }
    payload2, sig2 = sign(del_event_dict)
    res = await client.post("/webhooks/stripe", content=payload2, headers={"Stripe-Signature": sig2, "content-type": "application/json"})
    assert res.status_code == 200

    admin_headers = admin["headers"]

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "lite"
    assert data["status"] == "cancelled"

@pytest.mark.asyncio
async def test_client_cannot_declare_tier(client, db_session, admin, billing_settings):
    event_dict = {
        "id": "evt_5",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "customer": "cus_test123",
                "subscription": "sub_test3",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro",
                    "tier": "enterprise"  # malicious or confused client
                }
            }
        }
    }
    payload, sig = sign(event_dict)
    await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})

    admin_headers = admin["headers"]

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "pro"  # Not enterprise


@pytest.mark.asyncio
async def test_webhook_subscription_updated_past_due_falls_back_to_lite(client, db_session, admin, billing_settings):
    # Activate pro subscription
    checkout_event = {
        "id": "evt_upd1_chk",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_upd1",
                "customer": "cus_test123",
                "subscription": "sub_upd1",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro",
                },
            },
        },
    }
    payload, sig = sign(checkout_event)
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res.status_code == 200

    # Post customer.subscription.updated with status: past_due
    update_event = {
        "id": "evt_upd1_upd",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd1",
                "status": "past_due",
                "current_period_end": int(time.time()) + 86400,
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            },
        },
    }
    payload2, sig2 = sign(update_event)
    res2 = await client.post("/webhooks/stripe", content=payload2, headers={"Stripe-Signature": sig2, "content-type": "application/json"})
    assert res2.status_code == 200

    sub = (await db_session.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == "sub_upd1")
    )).scalar_one()
    assert sub.status == "past_due"

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin["headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "lite"
    assert data["status"] == "past_due"


@pytest.mark.asyncio
async def test_webhook_subscription_updated_price_change_changes_tier(client, db_session, admin, billing_settings):
    # Activate pro subscription
    checkout_event = {
        "id": "evt_upd2_chk",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_upd2",
                "customer": "cus_test123",
                "subscription": "sub_upd2",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro",
                },
            },
        },
    }
    payload, sig = sign(checkout_event)
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res.status_code == 200

    # Post update with price_ent, status active, future current_period_end
    future_ts = int(time.time()) + 86400
    update_event = {
        "id": "evt_upd2_upd",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd2",
                "status": "active",
                "current_period_end": future_ts,
                "items": {"data": [{"price": {"id": "price_ent"}}]},
            },
        },
    }
    payload2, sig2 = sign(update_event)
    res2 = await client.post("/webhooks/stripe", content=payload2, headers={"Stripe-Signature": sig2, "content-type": "application/json"})
    assert res2.status_code == 200

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin["headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "enterprise"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_webhook_subscription_updated_period_end_in_past_is_lite(client, db_session, admin, billing_settings):
    # Activate pro subscription
    checkout_event = {
        "id": "evt_upd3_chk",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_upd3",
                "customer": "cus_test123",
                "subscription": "sub_upd3",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro",
                },
            },
        },
    }
    payload, sig = sign(checkout_event)
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res.status_code == 200

    # Post update with status active but current_period_end in the past
    past_ts = int(time.time()) - 86400
    update_event = {
        "id": "evt_upd3_upd",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd3",
                "status": "active",
                "current_period_end": past_ts,
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            },
        },
    }
    payload2, sig2 = sign(update_event)
    res2 = await client.post("/webhooks/stripe", content=payload2, headers={"Stripe-Signature": sig2, "content-type": "application/json"})
    assert res2.status_code == 200

    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin["headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "lite"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_billing_webhook_writes_audit_event(client, db_session, admin, billing_settings):
    # Initial checkout.session.completed
    checkout_event = {
        "id": "evt_upd4_chk",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_upd4",
                "customer": "cus_test123",
                "subscription": "sub_upd4",
                "metadata": {
                    "org_id": admin["org_id"],
                    "price_id": "price_pro",
                },
            },
        },
    }
    payload, sig = sign(checkout_event)
    res = await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})
    assert res.status_code == 200

    # Assert an AuditEvent row exists with action == "billing_subscription_activated" for the org
    events = (await db_session.execute(
        select(AuditEvent).where(
            AuditEvent.org_id == uuid.UUID(admin["org_id"]),
            AuditEvent.action == "billing_subscription_activated",
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].event_metadata["product"] == "devgenie"
    assert events[0].event_metadata["tier"] == "pro"
    assert events[0].event_metadata["stripe_subscription_id"] == "sub_upd4"

    # Post customer.subscription.updated to exercise updated branch and verify billing_subscription_updated audit event
    update_event = {
        "id": "evt_upd4_upd",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd4",
                "status": "active",
                "current_period_end": int(time.time()) + 86400,
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            },
        },
    }
    payload2, sig2 = sign(update_event)
    res2 = await client.post("/webhooks/stripe", content=payload2, headers={"Stripe-Signature": sig2, "content-type": "application/json"})
    assert res2.status_code == 200

    upd_events = (await db_session.execute(
        select(AuditEvent).where(
            AuditEvent.org_id == uuid.UUID(admin["org_id"]),
            AuditEvent.action == "billing_subscription_updated",
        )
    )).scalars().all()
    assert len(upd_events) == 1
    assert upd_events[0].event_metadata["stripe_subscription_id"] == "sub_upd4"
    assert upd_events[0].event_metadata["status"] == "active"
