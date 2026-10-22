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
    """One instance per test (BUG-08). Tests seed Stripe's authoritative state explicitly; it is never derived
    from the event under test."""

    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []
        self.subscriptions_by_id = {}
        self.subscriptions_by_customer = {}
        self.open_sessions = []
        self.fail_retrieve = False
        self.fail_list = False

    def seed(self, sub_id, status="active", price="price_pro", period_end=None, top_period_end=None, cancel=None,
             customer="cus_test123"):
        """Stripe's current state for one subscription; `period_end` sits on the item, as in 2025+ API versions."""
        item = {"price": {"id": price}}
        if period_end is not None:
            item["current_period_end"] = period_end
        sub = {"id": sub_id, "customer": customer, "status": status, "items": {"data": [item]}}
        if top_period_end is not None:
            sub["current_period_end"] = top_period_end
        if cancel is not None:
            sub["cancel_at_period_end"] = cancel
        self.subscriptions_by_id[sub_id] = sub
        listed = [s for s in self.subscriptions_by_customer.get(customer, []) if s["id"] != sub_id]
        self.subscriptions_by_customer[customer] = listed + [sub]
        return sub

    def create_customer(self, name: str, email: str, metadata: dict) -> str:
        self.calls.append(("create_customer", name, email, metadata))
        return "cus_test123"

    def create_checkout_session(self, customer_id: str, price_id: str, success_url: str, cancel_url: str, metadata: dict) -> str:
        self.calls.append(("create_checkout_session", customer_id, price_id, metadata))
        self.open_sessions.append({"customer": customer_id, "url": "https://checkout.stripe.test/session", "metadata": dict(metadata)})
        return "https://checkout.stripe.test/session"

    def retrieve_subscription(self, subscription_id: str) -> dict:
        import copy
        import stripe
        self.calls.append(("retrieve_subscription", subscription_id))
        if self.fail_retrieve:
            raise stripe.error.APIConnectionError("stripe is down")
        if subscription_id not in self.subscriptions_by_id:
            raise stripe.error.InvalidRequestError(f"No such subscription: {subscription_id}", "id")
        return copy.deepcopy(self.subscriptions_by_id[subscription_id])

    def list_subscriptions(self, customer_id: str) -> list[dict]:
        import copy
        import stripe
        self.calls.append(("list_subscriptions", customer_id))
        if self.fail_list:
            raise stripe.error.APIConnectionError("stripe is down")
        return copy.deepcopy(self.subscriptions_by_customer.get(customer_id, []))

    def list_open_checkout_sessions(self, customer_id: str) -> list[dict]:
        import stripe
        self.calls.append(("list_open_checkout_sessions", customer_id))
        if self.fail_list:
            raise stripe.error.APIConnectionError("stripe is down")
        return [{"url": s["url"], "metadata": dict(s["metadata"])} for s in self.open_sessions if s["customer"] == customer_id]

    def create_portal_session(self, customer_id: str, return_url: str, cancel_subscription_id: str | None = None) -> str:
        self.calls.append(("create_portal_session", customer_id, cancel_subscription_id))
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
    gateway = FakeStripeGateway(get_test_settings())  # one per test, reused across requests (BUG-08)
    app.dependency_overrides[get_stripe_gateway] = lambda: gateway
    yield gateway
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
    billing_settings.seed("sub_test1")
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
    billing_settings.seed("sub_test1")
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
    billing_settings.seed("sub_test2")
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
    billing_settings.seed("sub_test2", status="canceled")
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
    billing_settings.seed("sub_test3")
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
    billing_settings.seed("sub_upd1")
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
    billing_settings.seed("sub_upd1", status="past_due", period_end=int(time.time()) + 86400)
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
    billing_settings.seed("sub_upd2")
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
    billing_settings.seed("sub_upd2", price="price_ent", period_end=future_ts)
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
    billing_settings.seed("sub_upd3")
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
    billing_settings.seed("sub_upd3", top_period_end=past_ts)
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
    billing_settings.seed("sub_upd4")
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
    gateway = billing_settings
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


# BUG-08 F4: Stripe retries the SAME bytes under the same event id. A case is either repairable on the server
# (the identical replay then activates exactly once) or irreparable (the identical replay stays refused).
# Each case: (event fields given the org id, setup before the first delivery, server repair before the replay).
def _no_op(ctx):
    return None


async def _create_org(ctx):
    ctx["db"].add(Organization(id=uuid.UUID(ctx["org_id"]), name="Late", slug="late-" + ctx["org_id"][:8]))
    await ctx["db"].commit()


REPAIRABLE = {
    "unknown_price": (lambda org: {"metadata": {"org_id": org, "price_id": "price_new"}}, _no_op,
                      lambda ctx: _with_price("price_new", "devgenie:pro")),
    "unknown_plan": (lambda org: {"metadata": {"org_id": org, "price_id": "price_gold"}},
                     lambda ctx: _with_price("price_gold", "devgenie:gold"),
                     lambda ctx: _with_price("price_gold", "devgenie:pro")),
    "unknown_org": (lambda org: {}, _no_op, _create_org),
    # Stripe had no answer for the subscription the first time; once it does, the same bytes activate.
    "stripe_unavailable": (lambda org: {}, lambda ctx: ctx["gw"].subscriptions_by_id.clear(),
                           lambda ctx: ctx["gw"].seed(ctx["sub"])),
}
IRREPARABLE = {
    "missing_org": (lambda org: {"metadata": {"price_id": "price_pro"}}, _no_op),
    "missing_customer": (lambda org: {"customer": None}, _no_op),
    "missing_subscription": (lambda org: {"subscription": None}, _no_op),
    "unpaid_session": (lambda org: {"payment_status": "unpaid"}, _no_op),
    "customer_mismatch": (lambda org: {}, lambda ctx: _set_customer(ctx["db"], ctx["org_id"], "cus_other")),
}
FAIL_CASES = {**REPAIRABLE, **IRREPARABLE}


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        await value


async def _devgenie_subs(db_session, org_id):
    return (await db_session.execute(select(Subscription).where(
        Subscription.org_id == uuid.UUID(org_id), Subscription.product == "devgenie"))).scalars().all()


async def _activated_audits(db_session, org_id):
    return (await db_session.execute(select(AuditEvent).where(
        AuditEvent.org_id == uuid.UUID(org_id), AuditEvent.action == "billing_subscription_activated"))).scalars().all()


async def _deliver_refused(client, db_session, ctx, event, reason):
    res = await _post(client, event)
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": reason}
    db_session.expire_all()
    assert await db_session.get(StripeEvent, event["id"]) is None  # not marked processed: the retry is not skipped
    assert await _devgenie_subs(db_session, ctx["org_id"]) == []


def _case_ctx(reason, admin, db_session, gateway):
    org_id = str(uuid.UUID(int=0xB08)) if reason == "unknown_org" else admin["org_id"]
    return {"org_id": org_id, "db": db_session, "gw": gateway, "sub": "sub_evt_fail_" + reason}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(REPAIRABLE))
async def test_checkout_that_cannot_activate_fails_loud_and_the_identical_retry_activates_once(client, db_session, admin, billing_settings, reason):
    fields, setup, repair = REPAIRABLE[reason]
    ctx = _case_ctx(reason, admin, db_session, billing_settings)
    billing_settings.seed(ctx["sub"])
    await _maybe_await(setup(ctx))
    event = _checkout_event("evt_fail_" + reason, ctx["org_id"], **fields(ctx["org_id"]))
    first_bytes = sign(event)[0]
    await _deliver_refused(client, db_session, ctx, event, reason)

    await _maybe_await(repair(ctx))
    assert sign(event)[0] == first_bytes  # the identical bytes, freshly signed
    for _ in range(2):  # Stripe's retry activates; a third delivery after success is a harmless no-op
        res = await _post(client, event)
        assert res.status_code == 200, res.text
    db_session.expire_all()
    subs = await _devgenie_subs(db_session, ctx["org_id"])
    assert [(s.status, s.source) for s in subs] == [("active", "stripe")]
    assert len(await _activated_audits(db_session, ctx["org_id"])) == 1
    assert await db_session.get(StripeEvent, event["id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(IRREPARABLE))
async def test_checkout_that_cannot_activate_stays_refused_on_identical_replay(client, db_session, admin, billing_settings, reason):
    fields, setup = IRREPARABLE[reason]
    ctx = _case_ctx(reason, admin, db_session, billing_settings)
    billing_settings.seed(ctx["sub"])
    await _maybe_await(setup(ctx))
    event = _checkout_event("evt_fail_" + reason, ctx["org_id"], **fields(ctx["org_id"]))
    for _ in range(2):  # the identical bytes, delivered again: still refused, still no effect
        await _deliver_refused(client, db_session, ctx, event, reason)
    assert await _activated_audits(db_session, ctx["org_id"]) == []


@pytest.mark.asyncio
async def test_a_fully_discounted_checkout_activates(client, db_session, admin, billing_settings):
    billing_settings.seed("sub_evt_promo")
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

    billing_settings.seed("sub_evt_chk")
    assert (await _post(client, _checkout_event("evt_chk", admin["org_id"]))).status_code == 200
    billing_settings.seed("sub_evt_chk", status="past_due" if event_type.endswith("updated") else "canceled")
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


# ---------------------------------------------------------------------------------------------------------
# BUG-08: subscription integrity. F1 one live subscription per org and product (checkout guard, conflicting
# activation); F2 Stripe's current state wins over event bodies; F3 a replacement carries its own period.
# Stripe's state is seeded on the per-test gateway; event bodies carry deliberately stale or contrary values.
# ---------------------------------------------------------------------------------------------------------
from datetime import datetime, timezone

from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService

FUTURE = int(time.time()) + 30 * 86400
LATER = FUTURE + 30 * 86400
PAST = int(time.time()) - 30 * 86400


def _checkout_url(admin):
    return f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session"


def _created(gateway):
    return [c for c in gateway.calls if c[0] == "create_checkout_session"]


def _updated_event(event_id, sub_id, **body):
    return {"id": event_id, "type": "customer.subscription.updated", "data": {"object": {"id": sub_id, **body}}}


def _deleted_event(event_id, sub_id, **body):
    return {"id": event_id, "type": "customer.subscription.deleted", "data": {"object": {"id": sub_id, **body}}}


def _epoch(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


async def _row(db_session, org_id):
    db_session.expire_all()
    [sub] = await _devgenie_subs(db_session, org_id)
    return sub


async def _put_row(db_session, org_id, sub_id, status, period_end, cancel):
    org = await db_session.get(Organization, uuid.UUID(org_id))
    org.stripe_customer_id = "cus_test123"
    pro = (await db_session.execute(select(Plan).where(Plan.product == "devgenie", Plan.code == "pro"))).scalar_one()
    db_session.add(Subscription(
        org_id=org.id, product="devgenie", plan_id=pro.id, status=status, source="stripe",
        stripe_customer_id="cus_test123", stripe_subscription_id=sub_id,
        current_period_end=datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None,
        cancel_at_period_end=cancel,
    ))
    await db_session.commit()


async def _activate_seeded(client, gateway, admin, event_id, **seed):
    gateway.seed("sub_" + event_id, **seed)
    res = await _post(client, _checkout_event(event_id, admin["org_id"]))
    assert res.status_code == 200, res.text
    return "sub_" + event_id


# --- F1: one live subscription per org and product --------------------------------------------------------

@pytest.mark.asyncio
async def test_bug08_f1_a_local_live_subscription_refuses_checkout(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _activate_seeded(client, gw, admin, "evt_f1a")
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_ent"})
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "subscription_exists"
    assert _created(gw) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["trialing", "active", "past_due", "unpaid", "incomplete", "paused"])
async def test_bug08_f1_a_stripe_listed_subscription_refuses_checkout_before_its_webhook(client, db_session, admin, billing_settings, live_status):
    gw = billing_settings
    await _set_customer(db_session, admin["org_id"], "cus_test123")
    gw.seed("sub_other_product", price="price_cloud")  # not this product: listed first, ignored
    gw.seed("sub_paid_no_webhook_yet", status=live_status, price="price_pro")
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "subscription_exists"
    assert _created(gw) == []


@pytest.mark.asyncio
async def test_bug08_f1_a_local_live_row_refuses_checkout_without_asking_stripe(client, db_session, admin, billing_settings):
    gw = billing_settings  # Stripe's listing is empty: only the local row can refuse
    await _put_row(db_session, admin["org_id"], "sub_local_live", status="active", period_end=FUTURE, cancel=False)
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "subscription_exists"
    assert not [c for c in gw.calls if c[0] in ("list_subscriptions", "list_open_checkout_sessions")]
    assert _created(gw) == []


@pytest.mark.asyncio
async def test_bug08_f1_open_sessions_are_listed_before_subscriptions(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _set_customer(db_session, admin["org_id"], "cus_test123")
    gw.open_sessions.append({"customer": "cus_test123", "url": "https://checkout.stripe.test/open-pro",
                             "metadata": {"org_id": admin["org_id"], "price_id": "price_pro"}})
    listed = gw.list_open_checkout_sessions

    def sessions_then_completion(customer_id):
        # The open session completes on Stripe between UAM's two calls: it leaves the open list and its
        # subscription appears. Listing sessions first means the subscription listing that follows sees it.
        result = listed(customer_id)
        gw.open_sessions.clear()
        gw.seed("sub_completed_between_calls")
        return result

    gw.list_open_checkout_sessions = sessions_then_completion
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    order = [c[0] for c in gw.calls if c[0] in ("list_subscriptions", "list_open_checkout_sessions")]
    assert order == ["list_open_checkout_sessions", "list_subscriptions"]
    assert res.status_code == 409, res.text
    assert _created(gw) == []


def _two_item_sub(sub_id, status="active", dev_price="price_ent", dev_period=None, other_period=None):
    """Another product's item first, DevGenie's second."""
    other = {"price": {"id": "price_cloud"}, **({"current_period_end": other_period} if other_period else {})}
    dev = {"price": {"id": dev_price}, **({"current_period_end": dev_period} if dev_period else {})}
    return {"id": sub_id, "customer": "cus_test123", "status": status, "items": {"data": [other, dev]}}


@pytest.mark.asyncio
async def test_bug08_f1_a_listed_subscription_whose_devgenie_item_is_not_first_refuses_checkout(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _set_customer(db_session, admin["org_id"], "cus_test123")
    gw.subscriptions_by_customer["cus_test123"] = [_two_item_sub("sub_bundle")]
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 409, res.text
    assert _created(gw) == []


@pytest.mark.asyncio
async def test_bug08_f2_reconcile_takes_devgenies_own_item_for_plan_and_period(client, db_session, admin, billing_settings):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2item", period_end=FUTURE)
    gw.subscriptions_by_id[sub] = _two_item_sub(sub, dev_price="price_ent", dev_period=LATER, other_period=PAST)
    assert (await _post(client, _updated_event("evt_f2item_upd", sub, status="active"))).status_code == 200
    row = await _row(db_session, admin["org_id"])
    plan = await db_session.get(Plan, row.plan_id)
    assert plan.code == "enterprise"
    assert _epoch(row.current_period_end) == LATER


@pytest.mark.asyncio
async def test_bug08_f2_a_same_id_update_without_a_flag_keeps_the_stored_flag(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _put_row(db_session, admin["org_id"], "sub_flagged", status="active", period_end=FUTURE, cancel=True)
    gw.seed("sub_flagged", period_end=FUTURE)  # Stripe's answer carries no cancel_at_period_end
    assert (await _post(client, _updated_event("evt_f2flag", "sub_flagged", status="active"))).status_code == 200
    assert (await _row(db_session, admin["org_id"])).cancel_at_period_end is True


@pytest.mark.asyncio
async def test_bug08_f1_an_open_session_for_this_product_is_reused(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _set_customer(db_session, admin["org_id"], "cus_test123")
    gw.open_sessions.append({"customer": "cus_test123", "url": "https://checkout.stripe.test/open-ent",
                             "metadata": {"org_id": admin["org_id"], "price_id": "price_ent"}})
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 200, res.text
    assert res.json() == {"url": "https://checkout.stripe.test/open-ent"}
    assert _created(gw) == []
    db_session.expire_all()
    reused = (await db_session.execute(select(AuditEvent).where(
        AuditEvent.org_id == uuid.UUID(admin["org_id"]), AuditEvent.action == "billing_checkout_reused"))).scalars().all()
    assert len(reused) == 1


@pytest.mark.asyncio
async def test_bug08_f1_two_sequential_checkouts_create_one_session(client, db_session, admin, billing_settings):
    gw = billing_settings
    for _ in range(2):
        res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
        assert res.status_code == 200, res.text
    assert len(_created(gw)) == 1


@pytest.mark.asyncio
async def test_bug08_f1_an_org_without_a_customer_lists_nothing_and_creates(client, db_session, admin, billing_settings):
    gw = billing_settings
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 200, res.text
    assert len(_created(gw)) == 1
    assert not [c for c in gw.calls if c[0] in ("list_subscriptions", "list_open_checkout_sessions")]


@pytest.mark.asyncio
async def test_bug08_f1_a_stripe_list_error_is_502_and_creates_nothing(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _set_customer(db_session, admin["org_id"], "cus_test123")
    gw.fail_list = True
    res = await client.post(_checkout_url(admin), headers=admin["headers"], json={"price_id": "price_pro"})
    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "stripe_unavailable"
    assert _created(gw) == []


@pytest.mark.asyncio
async def test_bug08_f1_activating_a_different_subscription_while_one_is_live_fails_loud(client, db_session, admin, billing_settings):
    gw = billing_settings
    first = await _activate_seeded(client, gw, admin, "evt_f1live")
    gw.seed("sub_evt_f1dup")
    res = await _post(client, _checkout_event("evt_f1dup", admin["org_id"]))
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": "conflicting_subscription"}
    assert (await _row(db_session, admin["org_id"])).stripe_subscription_id == first
    assert await db_session.get(StripeEvent, "evt_f1dup") is None


def test_bug08_f1_the_real_gateway_takes_cards_only(monkeypatch):
    seen = _capture(monkeypatch, _stripe.checkout.Session, "create", _types.SimpleNamespace(url="https://checkout.stripe.test/s"))
    StripeGateway(get_settings()).create_checkout_session(
        customer_id="cus_1", price_id="price_pro", success_url="http://s", cancel_url="http://c", metadata={"org_id": "o"}
    )
    assert seen.get("payment_method_types") == ["card"]


class _Pages:
    """A Stripe list result: auto_paging_iter() walks every page; the first page alone does not."""

    def __init__(self, *pages):
        self.pages = pages
        self.data = list(pages[0])

    def auto_paging_iter(self):
        for page in self.pages:
            yield from page


class _Obj(dict):
    def to_dict(self):
        return dict(self)


def test_bug08_f1_the_real_gateway_lists_every_page_of_subscriptions_and_open_sessions(monkeypatch):
    sub_seen = _capture(monkeypatch, _stripe.Subscription, "list",
                        _Pages([_Obj(id="sub_1", status="canceled")], [_Obj(id="sub_2", status="active")]))
    ses_seen = _capture(monkeypatch, _stripe.checkout.Session, "list",
                        _Pages([], [_Obj(id="cs_2", url="https://checkout.stripe.test/2", metadata={"price_id": "price_pro"})]))
    gw = StripeGateway(get_settings())
    assert hasattr(gw, "list_subscriptions") and hasattr(gw, "list_open_checkout_sessions")
    assert [s["id"] for s in gw.list_subscriptions("cus_1")] == ["sub_1", "sub_2"]
    assert sub_seen["customer"] == "cus_1" and sub_seen["status"] == "all"
    assert gw.list_open_checkout_sessions("cus_1") == [{"url": "https://checkout.stripe.test/2", "metadata": {"price_id": "price_pro"}}]
    assert ses_seen["customer"] == "cus_1" and ses_seen["status"] == "open"


def test_bug08_the_real_gateway_retrieves_a_subscription_as_a_dict(monkeypatch):
    monkeypatch.setattr(_stripe.Subscription, "retrieve", lambda sid: _Obj(id=sid, status="active"))
    gw = StripeGateway(get_settings())
    assert hasattr(gw, "retrieve_subscription")
    assert gw.retrieve_subscription("sub_9") == {"id": "sub_9", "status": "active"}


# --- F2: Stripe's current state wins over the event body ---------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("same_created", [False, True])
async def test_bug08_f2_a_stale_active_update_after_cancel_stays_cancelled(client, db_session, admin, billing_settings, same_created):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2c", period_end=FUTURE)
    gw.seed(sub, status="canceled", period_end=FUTURE)
    stamp = {"created": 1_760_000_000} if same_created else {}
    deleted = {**_deleted_event("evt_f2c_del", sub, status="canceled"), **stamp}
    stale = {**_updated_event("evt_f2c_stale", sub, status="active", current_period_end=FUTURE), **stamp}
    assert (await _post(client, deleted)).status_code == 200
    assert (await _post(client, stale)).status_code == 200
    assert (await _row(db_session, admin["org_id"])).status == "cancelled"


@pytest.mark.asyncio
async def test_bug08_f2_a_cancel_flag_update_after_a_renewal_keeps_stripes_period_and_flag(client, db_session, admin, billing_settings):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2r", period_end=FUTURE)
    gw.seed(sub, period_end=LATER, cancel=True)  # renewed, then set to cancel at the (new) period end
    renewal = _updated_event("evt_f2r_renew", sub, status="active", cancel_at_period_end=False,
                             items={"data": [{"price": {"id": "price_pro"}, "current_period_end": LATER}]})
    old_flag = _updated_event("evt_f2r_flag", sub, status="active", cancel_at_period_end=True,
                              items={"data": [{"price": {"id": "price_pro"}, "current_period_end": FUTURE}]})
    assert (await _post(client, renewal)).status_code == 200
    assert (await _post(client, old_flag)).status_code == 200  # an older snapshot delivered last
    row = await _row(db_session, admin["org_id"])
    assert _epoch(row.current_period_end) == LATER
    assert row.cancel_at_period_end is True


@pytest.mark.asyncio
async def test_bug08_f2_a_late_checkout_for_a_cancelled_subscription_lands_cancelled(client, db_session, admin, billing_settings):
    gw = billing_settings
    gw.seed("sub_evt_f2late", status="canceled", period_end=PAST)
    assert (await _post(client, _checkout_event("evt_f2late", admin["org_id"]))).status_code == 200
    assert (await _row(db_session, admin["org_id"])).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_event", [_updated_event, _deleted_event], ids=["updated", "deleted"])
async def test_bug08_f2_stripe_unreachable_on_an_update_is_500_with_no_effect(client, db_session, admin, billing_settings, make_event):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2down")
    gw.fail_retrieve = True
    res = await _post(client, make_event("evt_f2down_upd", sub, status="canceled"))
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": "stripe_unavailable"}
    assert (await _row(db_session, admin["org_id"])).status == "active"
    assert await db_session.get(StripeEvent, "evt_f2down_upd") is None
    effects = (await db_session.execute(select(AuditEvent).where(
        AuditEvent.org_id == uuid.UUID(admin["org_id"]),
        AuditEvent.action.in_(("billing_subscription_updated", "billing_subscription_cancelled"))))).scalars().all()
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [{"customer": "cus_someone_else"}, {"price": "price_cloud"}])
async def test_bug08_f2_a_retrieved_subscription_that_is_not_ours_is_refused(client, db_session, admin, billing_settings, mismatch):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2mis")
    gw.seed(sub, status="past_due", **mismatch)
    res = await _post(client, _updated_event("evt_f2mis_upd", sub, status="past_due"))
    assert res.status_code == 500, res.text
    assert res.json()["error"] == {"code": "activation_failed", "message": "stripe_mismatch"}
    assert (await _row(db_session, admin["org_id"])).status == "active"
    assert await db_session.get(StripeEvent, "evt_f2mis_upd") is None


STRIPE_TO_UAM = {"active": "active", "trialing": "trialing", "past_due": "past_due", "unpaid": "past_due",
                 "incomplete": "past_due", "paused": "past_due", "canceled": "cancelled", "incomplete_expired": "cancelled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stripe_status", list(STRIPE_TO_UAM))
async def test_bug08_f2_every_stripe_status_lands_on_its_uam_status(client, db_session, admin, billing_settings, stripe_status):
    gw = billing_settings
    sub = await _activate_seeded(client, gw, admin, "evt_f2s")
    gw.seed(sub, status=stripe_status)
    # The body says "active" whatever Stripe now reports: only the retrieved status may count.
    assert (await _post(client, _updated_event("evt_f2s_upd", sub, status="active"))).status_code == 200
    assert (await _row(db_session, admin["org_id"])).status == STRIPE_TO_UAM[stripe_status]


# --- F3: a replacement subscription carries its own period ------------------------------------------------

@pytest.mark.asyncio
async def test_bug08_f3_a_resubscription_after_expiry_carries_its_own_period_and_is_paid_at_once(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _put_row(db_session, admin["org_id"], "sub_old", status="cancelled", period_end=PAST, cancel=True)
    gw.seed("sub_evt_f3new", status="active", period_end=FUTURE)  # no cancel flag in Stripe's answer
    assert (await _post(client, _checkout_event("evt_f3new", admin["org_id"]))).status_code == 200
    row = await _row(db_session, admin["org_id"])
    assert row.stripe_subscription_id == "sub_evt_f3new"
    assert row.status == "active"
    assert row.current_period_end is not None and _epoch(row.current_period_end) == FUTURE
    assert row.cancel_at_period_end is False
    summary = (await client.get(f"/api/v1/orgs/{admin['org_id']}/billing", headers=admin["headers"])).json()["subscriptions"][0]
    assert summary["status"] == "active" and summary["cancel_at_period_end"] is False
    settings = get_settings()
    ent = await EntitlementService(db_session, settings, AuditService(db_session, settings)).resolve(admin["org_id"], "devgenie")
    assert ent.tier == "pro"  # the internal entitlement answer (by-subject resolves through this) is paid at once


@pytest.mark.asyncio
async def test_bug08_f3_a_later_event_for_the_replaced_subscription_is_unknown(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _put_row(db_session, admin["org_id"], "sub_old", status="cancelled", period_end=PAST, cancel=True)
    gw.seed("sub_evt_f3rep", status="active", period_end=FUTURE)
    assert (await _post(client, _checkout_event("evt_f3rep", admin["org_id"]))).status_code == 200
    gw.seed("sub_old", status="active")
    res = await _post(client, _updated_event("evt_f3_old", "sub_old", status="active"))
    assert res.status_code == 500 and res.json()["error"]["message"] == "unknown_subscription"


@pytest.mark.asyncio
async def test_bug08_f3_a_replacement_with_no_period_has_none(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _put_row(db_session, admin["org_id"], "sub_old", status="cancelled", period_end=PAST, cancel=True)
    gw.seed("sub_evt_f3none", status="active")
    assert (await _post(client, _checkout_event("evt_f3none", admin["org_id"]))).status_code == 200
    assert (await _row(db_session, admin["org_id"])).current_period_end is None


@pytest.mark.asyncio
async def test_bug08_f3_a_same_id_reactivation_with_no_period_keeps_the_stored_period(client, db_session, admin, billing_settings):
    gw = billing_settings
    await _put_row(db_session, admin["org_id"], "sub_evt_f3same", status="active", period_end=FUTURE, cancel=False)
    gw.seed("sub_evt_f3same", status="active")
    assert (await _post(client, _checkout_event("evt_f3same", admin["org_id"]))).status_code == 200
    row = await _row(db_session, admin["org_id"])
    assert _epoch(row.current_period_end) == FUTURE
