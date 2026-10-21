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

    def create_customer(self, name: str, email: str, metadata: dict) -> str:
        self.calls.append(("create_customer", name, email, metadata))
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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
                "payment_status": "paid",
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


# ---------------------------------------------------------------------------------------------------------
# BRIEF-019: the REAL StripeGateway, with only the Stripe library calls stubbed. The fake gateway above
# replaces the class, so it cannot prove what is sent to Stripe.
# ---------------------------------------------------------------------------------------------------------
import types as _types

import stripe as _stripe


def _capture(monkeypatch, target, attr, result):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return result

    monkeypatch.setattr(target, attr, create)
    return seen


def test_checkout_session_offers_promotion_codes_and_always_collects_a_card(monkeypatch):
    seen = _capture(monkeypatch, _stripe.checkout.Session, "create", _types.SimpleNamespace(url="https://checkout.stripe.test/s"))
    url = StripeGateway(get_settings()).create_checkout_session(
        customer_id="cus_1", price_id="price_pro", success_url="http://s", cancel_url="http://c", metadata={"org_id": "o"}
    )
    assert url == "https://checkout.stripe.test/s"
    assert seen["allow_promotion_codes"] is True
    assert "payment_method_collection" not in seen  # Stripe's default: a card even on a 100%-off code


def test_a_new_stripe_customer_carries_the_buyers_email(monkeypatch):
    seen = _capture(monkeypatch, _stripe.Customer, "create", _types.SimpleNamespace(id="cus_new"))
    cid = StripeGateway(get_settings()).create_customer(name="Acme", email="buyer@example.com", metadata={"org_id": "o"})
    assert cid == "cus_new"
    assert seen == {"name": "Acme", "email": "buyer@example.com", "metadata": {"org_id": "o"}}


@pytest.mark.asyncio
async def test_checkout_creates_the_customer_with_the_admins_email_once(client, db_session, admin, billing_settings):
    gateway = FakeStripeGateway(get_settings())
    app.dependency_overrides[get_stripe_gateway] = lambda: gateway
    path = f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session"
    for _ in range(2):
        res = await client.post(path, headers=admin["headers"], json={"price_id": "price_pro"})
        assert res.status_code == 200, res.text
    created = [c for c in gateway.calls if c[0] == "create_customer"]
    assert len(created) == 1  # an existing customer is left as it is
    assert created[0][2] == "admin@example.com"


# ---------------------------------------------------------------------------------------------------------
# BRIEF-023 (CR-214 slice 1): a paid checkout is never lost. An event that cannot be applied answers 500
# activation_failed with a reason, stores NO StripeEvent row and changes no subscription, so Stripe's retry
# of the SAME event id is processed afresh once the data is put right, and activates exactly once.
# ---------------------------------------------------------------------------------------------------------
import logging

from app.models import Organization, StripeEvent


async def _post(client, event):
    payload, sig = sign(event)
    return await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": sig, "content-type": "application/json"})


def _checkout_event(event_id, org_id, **fields):
    obj = {
        "id": "cs_" + event_id,
        "customer": "cus_test123",
        "subscription": "sub_" + event_id,
        "payment_status": "paid",
        "metadata": {"org_id": org_id, "price_id": "price_pro"},
    }
    obj.update(fields)
    return {"id": event_id, "type": "checkout.session.completed", "data": {"object": obj}}


def _with_price(price_id, target):
    base = app.dependency_overrides[get_settings]

    def settings():  # a copy: the fake gateway's override resets the shared cached Settings
        s = base()
        return s.model_copy(update={"STRIPE_PRICE_MAP": {**s.STRIPE_PRICE_MAP, price_id: target}})

    app.dependency_overrides[get_settings] = settings


async def _set_customer(db_session, org_id, customer_id):
    org = await db_session.get(Organization, uuid.UUID(org_id))
    org.stripe_customer_id = customer_id
    await db_session.commit()


# Each case: (reason, bad event fields, setup before the bad post, good event fields, correction before replay).
def _no_op(admin, db_session):
    return None


FAIL_CASES = {
    "unknown_price": (lambda a: {"metadata": {"org_id": a["org_id"], "price_id": "price_new"}}, _no_op,
                      lambda a: {"metadata": {"org_id": a["org_id"], "price_id": "price_new"}},
                      lambda a, db: _with_price("price_new", "devgenie:pro")),
    "unknown_plan": (lambda a: {"metadata": {"org_id": a["org_id"], "price_id": "price_gold"}},
                     lambda a, db: _with_price("price_gold", "devgenie:gold"),
                     lambda a: {"metadata": {"org_id": a["org_id"], "price_id": "price_gold"}},
                     lambda a, db: _with_price("price_gold", "devgenie:pro")),
    "missing_org": (lambda a: {"metadata": {"price_id": "price_pro"}}, _no_op, lambda a: {}, _no_op),
    "unknown_org": (lambda a: {"metadata": {"org_id": str(uuid.uuid4()), "price_id": "price_pro"}}, _no_op,
                    lambda a: {}, _no_op),
    "missing_customer": (lambda a: {"customer": None}, _no_op, lambda a: {}, _no_op),
    "missing_subscription": (lambda a: {"subscription": None}, _no_op, lambda a: {}, _no_op),
    "unpaid_session": (lambda a: {"payment_status": "unpaid"}, _no_op, lambda a: {}, _no_op),
    "customer_mismatch": (lambda a: {}, lambda a, db: _set_customer(db, a["org_id"], "cus_other"),
                          lambda a: {"customer": "cus_other"}, _no_op),
}


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        await value


async def _devgenie_subs(db_session, org_id):
    return (await db_session.execute(select(Subscription).where(
        Subscription.org_id == uuid.UUID(org_id), Subscription.product == "devgenie"))).scalars().all()


async def _activated_audits(db_session, org_id):
    return (await db_session.execute(select(AuditEvent).where(
        AuditEvent.org_id == uuid.UUID(org_id), AuditEvent.action == "billing_subscription_activated"))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(FAIL_CASES))
async def test_checkout_that_cannot_activate_fails_loud_and_the_retry_activates_once(client, db_session, admin, billing_settings, reason):
    bad, setup, good, correct = FAIL_CASES[reason]
    event_id = "evt_fail_" + reason
    await _maybe_await(setup(admin, db_session))

    res = await _post(client, _checkout_event(event_id, admin["org_id"], **bad(admin)))
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": reason}
    db_session.expire_all()
    assert await db_session.get(StripeEvent, event_id) is None  # not marked processed: the retry is not skipped
    assert await _devgenie_subs(db_session, admin["org_id"]) == []

    await _maybe_await(correct(admin, db_session))
    replay = _checkout_event(event_id, admin["org_id"], **good(admin))
    for _ in range(2):  # Stripe's retry activates; a duplicate after success stays a harmless 200
        res = await _post(client, replay)
        assert res.status_code == 200, res.text
    db_session.expire_all()
    subs = await _devgenie_subs(db_session, admin["org_id"])
    assert [(s.status, s.source) for s in subs] == [("active", "stripe")]
    assert len(await _activated_audits(db_session, admin["org_id"])) == 1
    assert await db_session.get(StripeEvent, event_id) is not None


@pytest.mark.asyncio
async def test_a_fully_discounted_checkout_activates(client, db_session, admin, billing_settings):
    res = await _post(client, _checkout_event("evt_promo", admin["org_id"], payment_status="no_payment_required"))
    assert res.status_code == 200, res.text
    assert [s.status for s in await _devgenie_subs(db_session, admin["org_id"])] == ["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["customer.subscription.updated", "customer.subscription.deleted"])
async def test_subscription_event_before_its_checkout_fails_loud_then_applies(client, db_session, admin, billing_settings, event_type):
    early = {"id": "evt_early", "type": event_type, "data": {"object": {"id": "sub_evt_chk", "status": "past_due"}}}
    res = await _post(client, early)
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": "unknown_subscription"}
    db_session.expire_all()
    assert await db_session.get(StripeEvent, "evt_early") is None

    assert (await _post(client, _checkout_event("evt_chk", admin["org_id"]))).status_code == 200
    res = await _post(client, early)  # Stripe's retry, after the checkout landed
    assert res.status_code == 200, res.text
    db_session.expire_all()
    [sub] = await _devgenie_subs(db_session, admin["org_id"])
    assert sub.status == ("past_due" if event_type.endswith("updated") else "cancelled")


@pytest.mark.asyncio
async def test_activation_failure_logs_an_error_without_the_payload(client, db_session, admin, billing_settings, caplog):
    caplog.set_level(logging.ERROR)
    res = await _post(client, _checkout_event("evt_log", admin["org_id"], payment_status="unpaid"))
    assert res.status_code == 500
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    line = errors[0].getMessage()
    for part in ("evt_log", "checkout.session.completed", "unpaid_session", admin["org_id"]):
        assert part in line
    for secret in ("cus_test123", "whsec_test", "cs_evt_log"):
        assert secret not in line


@pytest.mark.asyncio
async def test_an_unhandled_event_type_is_a_recorded_no_op(client, db_session, billing_settings):
    res = await _post(client, {"id": "evt_other", "type": "invoice.created", "data": {"object": {}}})
    assert res.status_code == 200
    db_session.expire_all()
    assert await db_session.get(StripeEvent, "evt_other") is not None
