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


async def _put_row(db_session, org_id, sub_id, status, period_end, cancel, customer="cus_test123"):
    org = await db_session.get(Organization, uuid.UUID(org_id))
    org.stripe_customer_id = customer
    pro = (await db_session.execute(select(Plan).where(Plan.product == "devgenie", Plan.code == "pro"))).scalar_one()
    db_session.add(Subscription(
        org_id=org.id, product="devgenie", plan_id=pro.id, status=status, source="stripe",
        stripe_customer_id=customer, stripe_subscription_id=sub_id,
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


# ---------------------------------------------------------------------------------------------------------
# BRIEF-184 / XP-CR020-TESTER-CODES: W1-W6. These are RED-first witnesses, not a tester implementation.
# StripeGateway and BillingService remain real. Only Stripe's SDK boundary is replaced; all uncaptured SDK
# requests fail. The mutant catalogue is tests/fixtures/b184-mutants.json. No test runs a child process.
# ---------------------------------------------------------------------------------------------------------
import asyncio
import copy
import importlib
import importlib.util
import os
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.session import get_session
from app.models import ExternalIdentity, User
from app.models.enums import ExternalProvider
from app.models.plan import DEFAULT_PLANS
from app.security.jwt import create_access_token

B184_ROOT = Path(__file__).resolve().parents[1]
B184_REPLIES = json.loads((B184_ROOT / "tests/fixtures/b184-uam-replies.json").read_text())
B184_START = datetime(2096, 1, 31, 12, 30, 45, tzinfo=timezone.utc)
B184_END = datetime(2096, 2, 29, 12, 30, 45, tzinfo=timezone.utc)
B184_PRICE = "price_b184_zero"
B184_CODE = "B184-Tester"
B184_SECRET = "b184-service-secret-for-tests-only"


class _TesterObject(dict):
    """Stripe-shaped objects, without permitting a fallback SDK/network call."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def to_dict(self):
        return dict(self)

    def to_dict_recursive(self):
        return dict(self)


def _tester_object(value):
    if isinstance(value, dict):
        return _TesterObject({k: _tester_object(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_tester_object(v) for v in value]
    return value


class _TesterPages(_TesterObject):
    def __init__(self, pages):
        super().__init__(data=pages[0], has_more=len(pages) > 1, object="list")
        self.pages = pages

    def auto_paging_iter(self):
        for page in self.pages:
            yield from page


class _TesterStripeSDK:
    """External state machine ONLY: records the real gateway's wire arguments.

    Atomic cap/idempotency here models Stripe, not evidence of Stripe feasibility (V0 is still required).
    It deliberately does not check UAM eligibility or enforce UAM user/org uniqueness.
    """

    def __init__(self, monkeypatch, settings):
        self.settings = settings
        self.now = B184_START
        self.calls = []
        self.creates = []
        self.successes = []
        self.cache = {}
        self.subscriptions = {}
        self.customers = {}
        self.open_sessions = []
        self.fail = None
        self.fail_lookup = False
        self.price = {
            "id": B184_PRICE, "active": True, "currency": "gbp", "unit_amount": 0,
            "unit_amount_decimal": "0", "billing_scheme": "per_unit", "type": "recurring",
            "product": "prod_b184", "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"},
        }
        self.promo = {
            "id": "promo_b184", "code": B184_CODE, "active": True, "metadata": {"tester": "true"},
            "expires_at": int((B184_START + timedelta(days=10)).timestamp()),
            "max_redemptions": 10, "times_redeemed": 0, "customer": None,
            "restrictions": {"first_time_transaction": False, "minimum_amount": None, "minimum_amount_currency": None},
            "coupon": {"id": "coupon_b184", "valid": True, "percent_off": 100, "amount_off": None,
                       "duration": "once", "applies_to": {"products": ["prod_b184"]}},
        }
        self.promos = [self.promo]
        self._install(monkeypatch)

    def _install(self, monkeypatch):
        def deny(*args, **kwargs):
            raise AssertionError("B184: an unmocked Stripe SDK/network action was attempted")
        # All genuine network operations ultimately pass this SDK seam.
        from stripe._api_requestor import _APIRequestor
        monkeypatch.setattr(_APIRequestor, "request", deny)
        monkeypatch.setattr(_APIRequestor, "request_async", deny)
        for target, name, fn in [
            (_stripe.Price, "retrieve", self.retrieve_price),
            (_stripe.PromotionCode, "list", self.list_promos),
            (_stripe.PromotionCode, "retrieve", self.retrieve_promo),
            (_stripe.Coupon, "retrieve", self.retrieve_coupon),
            (_stripe.Customer, "create", self.create_customer),
            (_stripe.Customer, "retrieve", self.retrieve_customer),
            (_stripe.Subscription, "create", self.create_subscription),
            (_stripe.Subscription, "retrieve", self.retrieve_subscription),
            (_stripe.Subscription, "list", self.list_subscriptions),
            (_stripe.checkout.Session, "list", self.list_sessions),
            (_stripe.checkout.Session, "create", self.create_checkout),
            (_stripe.Invoice, "retrieve", self.retrieve_invoice),
            (_stripe.InvoiceItem, "list", lambda **kw: _TesterPages([[]])),
            (_stripe.PaymentMethod, "list", lambda **kw: _TesterPages([[]])),
        ]:
            monkeypatch.setattr(target, name, fn)
        # These are forbidden even if an implementation adds an SDK call outside the proposed writer.
        for target, method in [
            (_stripe.PaymentMethod, "attach"), (_stripe.Customer, "modify"), (_stripe.Customer, "create_source"),
            (_stripe.PaymentIntent, "create"), (_stripe.PaymentIntent, "confirm"),
            (_stripe.Charge, "create"), (_stripe.Invoice, "pay"), (_stripe.Subscription, "modify"),
        ]:
            monkeypatch.setattr(target, method, deny)

    def retrieve_price(self, price_id, **kwargs):
        self.calls.append(("price.retrieve", price_id))
        return _tester_object(copy.deepcopy(self.price))

    def list_promos(self, **kwargs):
        self.calls.append(("promotion.list", copy.deepcopy(kwargs)))
        # Eligible code is on the SECOND page; unrelated first-page result must not win.
        decoy = {**copy.deepcopy(self.promo), "id": "promo_other", "code": "OTHER"}
        return _TesterPages([[_tester_object(decoy)], _tester_object(copy.deepcopy(self.promos))])

    def retrieve_promo(self, promo_id, **kwargs):
        self.calls.append(("promotion.retrieve", promo_id))
        return _tester_object(copy.deepcopy(next(p for p in self.promos if p["id"] == promo_id)))

    def retrieve_coupon(self, coupon_id, **kwargs):
        return _tester_object(copy.deepcopy(self.promo["coupon"]))

    def create_customer(self, **kwargs):
        cid = "cus_b184_" + str(len(self.customers) + 1)
        self.calls.append(("customer.create", copy.deepcopy(kwargs)))
        self.customers[cid] = {"id": cid, "balance": 0, "default_source": None,
                               "invoice_settings": {"default_payment_method": None}}
        return _tester_object(copy.deepcopy(self.customers[cid]))

    def retrieve_customer(self, customer_id, **kwargs):
        self.calls.append(("customer.retrieve", customer_id))
        return _tester_object(copy.deepcopy(self.customers[customer_id]))

    def saved_customer(self):
        cid = "cus_b184_saved"
        self.customers[cid] = {"id": cid, "balance": 0, "default_source": "card_saved",
                               "invoice_settings": {"default_payment_method": "pm_saved"}}
        return cid

    def create_subscription(self, **kwargs):
        wire = copy.deepcopy(kwargs)
        self.creates.append(wire)
        self.calls.append(("subscription.create", wire))
        key = kwargs.get("idempotency_key")
        assert key, "new-idempotency-key: every write needs a durable reservation key"
        if key in self.cache:
            old, result = self.cache[key]
            assert old == wire, "refresh-retry-end: idempotent retry changed the original payload"
            return _tester_object(copy.deepcopy(result))
        if self.fail == "before":
            self.fail = None
            raise _stripe.error.APIConnectionError("B184 timeout before creation")
        # A missing discount intentionally bypasses the fake cap, exposing omit-discount.
        if kwargs.get("discounts") == [{"promotion_code": self.promo["id"]}]:
            cap, count = self.promo.get("max_redemptions"), self.promo.get("times_redeemed")
            if type(cap) is int and type(count) is int and count >= cap:
                raise _stripe.error.InvalidRequestError("Promotion code exhausted", "discounts")
            if type(count) is int:
                self.promo["times_redeemed"] += 1
        sid = "sub_b184_" + str(len(self.successes) + 1)
        end = kwargs.get("cancel_at")
        result = {
            "id": sid, "customer": kwargs["customer"], "status": "active",
            "items": {"data": [{"id": "si_" + sid, "price": copy.deepcopy(self.price),
                                "current_period_end": end}]},
            "cancel_at": end, "cancel_at_period_end": False, "current_period_end": end,
            "metadata": copy.deepcopy(kwargs.get("metadata", {})),
            "discounts": [{"id": "di_b184", "promotion_code": self.promo["id"],
                           "coupon": copy.deepcopy(self.promo["coupon"])}],
            "discount": {"promotion_code": self.promo["id"], "coupon": copy.deepcopy(self.promo["coupon"])},
            "collection_method": kwargs.get("collection_method"),
            "latest_invoice": {"id": "in_" + sid, "total": 0, "subtotal": 0, "amount_due": 0,
                               "amount_paid": 0, "amount_remaining": 0, "status": "paid",
                               "currency": "gbp", "charge": None, "payment_intent": None},
        }
        self.subscriptions[sid] = result
        self.cache[key] = (wire, copy.deepcopy(result))
        self.successes.append(sid)
        if self.fail == "after":
            self.fail = None
            raise _stripe.error.APIConnectionError("B184 response lost after success")
        return _tester_object(copy.deepcopy(result))

    def retrieve_subscription(self, subscription_id, **kwargs):
        self.calls.append(("subscription.retrieve", subscription_id))
        if self.fail_lookup:
            raise _stripe.error.APIConnectionError("B184 lookup unavailable")
        return _tester_object(copy.deepcopy(self.subscriptions[subscription_id]))

    def list_subscriptions(self, **kwargs):
        self.calls.append(("subscription.list", copy.deepcopy(kwargs)))
        if self.fail_lookup:
            raise _stripe.error.APIConnectionError("B184 lookup unavailable")
        subs = [s for s in self.subscriptions.values() if s["customer"] == kwargs["customer"]]
        return _TesterPages([[], _tester_object(copy.deepcopy(subs))])

    def list_sessions(self, **kwargs):
        self.calls.append(("checkout.list", copy.deepcopy(kwargs)))
        return _TesterPages([[], _tester_object([s for s in self.open_sessions if s["customer"] == kwargs["customer"]])])

    def create_checkout(self, **kwargs):
        self.calls.append(("checkout.create", copy.deepcopy(kwargs)))
        result = {"id": "cs_b184_paid", "url": "https://checkout.stripe.test/b184",
                  "customer": kwargs["customer"], "metadata": kwargs["metadata"]}
        self.open_sessions.append(result)
        return _tester_object(result)

    def retrieve_invoice(self, invoice_id, **kwargs):
        return _tester_object(next(s["latest_invoice"] for s in self.subscriptions.values()
                                   if s["latest_invoice"]["id"] == invoice_id))


@pytest.fixture
def tester_sdk(monkeypatch):
    monkeypatch.setattr(_stripe, "api_key", _stripe.api_key)
    settings = get_settings().model_copy(update={
        "STRIPE_API_KEY": "sk_test_b184_fake_no_network", "STRIPE_WEBHOOK_SECRET": "whsec_test",
        "STRIPE_TESTER_PRICE_ID": B184_PRICE,
        "STRIPE_PRICE_MAP": {"price_pro": "devgenie:pro", "price_ent": "devgenie:enterprise"},
        "STRIPE_CHECKOUT_SUCCESS_URL": "http://test/success", "STRIPE_CHECKOUT_CANCEL_URL": "http://test/cancel",
        "DEVGENIE_SERVICE_SECRET": B184_SECRET,
    })
    sdk = _TesterStripeSDK(monkeypatch, settings)
    import app.services.billing_service as billing_module
    import app.services.entitlement_service as entitlement_module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return sdk.now.astimezone(tz) if tz else sdk.now.replace(tzinfo=None)

    monkeypatch.setattr(billing_module, "datetime", Clock)
    monkeypatch.setattr(billing_module, "utcnow", lambda: sdk.now, raising=False)
    monkeypatch.setattr(entitlement_module, "utcnow", lambda: sdk.now)
    # A future implementation may use the shared clock through the module rather than an imported name.
    import app.utils.time as time_module
    monkeypatch.setattr(time_module, "utcnow", lambda: sdk.now)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_stripe_gateway] = lambda: StripeGateway(settings)
    yield sdk
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_stripe_gateway, None)


@pytest.fixture
async def tester_admin(fresh_client, sent_otps, tester_sdk):
    return await _signup(fresh_client, sent_otps, "b184@example.com")


def _tester_url(actor):
    return f"/api/v1/orgs/{actor['org_id']}/billing/tester-code"


async def _redeem(client, actor, code=B184_CODE, **extra):
    return await client.post(_tester_url(actor), headers=actor["headers"], json={"code": code, **extra})


def _tester_refusal(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}, body
    assert body["error"]["code"] == code and isinstance(body["error"]["message"], str), body


def _tester_model():
    import app.models as models
    assert hasattr(models, "TesterRedemption"), "W6: the durable TesterRedemption model is not registered"
    return models.TesterRedemption


async def _tester_rows(factory):
    model = _tester_model()
    async with factory() as session:
        return list((await session.execute(select(model))).scalars())


async def _tester_no_effect(factory, sdk):
    assert sdk.creates == [], "refused input reached Subscription.create"
    async with factory() as session:
        assert list((await session.execute(select(Subscription))).scalars()) == []
    assert not [r for r in await _tester_rows(factory) if r.state == "applied"]


async def _tester_applied(factory, actor, sdk, end=B184_END):
    rows = await _tester_rows(factory)
    [row] = [r for r in rows if str(r.user_id) == actor["user_id"]]
    assert row.state == "applied" and row.ends_at == end
    assert row.stripe_subscription_id in sdk.successes
    assert str(row.org_id) == actor["org_id"] and row.tester_price_id == B184_PRICE
    async with factory() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.org_id == row.org_id))).scalar_one()
        assert sub.source == "stripe" and sub.status == "active"
        assert sub.current_period_end == end and sub.cancel_at_period_end is True
        ent = await EntitlementService(session, sdk.settings, AuditService(session, sdk.settings)).resolve(actor["org_id"], "devgenie")
        assert ent.tier == "pro" and ent.expires_at == end
    return row


def _tester_success(response, end=B184_END):
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"status", "tier", "ends_at"}
    assert body["status"] == "active" and body["tier"] == "pro"
    assert datetime.fromisoformat(body["ends_at"].replace("Z", "+00:00")) == end


def _tester_capture(name, response):
    actual = {"status": response.status_code, "body": response.json()}
    assert actual == B184_REPLIES[name], f"W3/W5 cross-repository contract mismatch: {actual}"
    print("B184 " + name + " " + json.dumps(actual, sort_keys=True))
    # Co-arch opts in to capturing REAL replies. Portal replays these exact files when this directory is set.
    target = os.environ.get("B184_CAPTURE_DIR")
    if target:
        folder = Path(target)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name + ".json")).write_text(json.dumps(actual, indent=2) + "\n")


@pytest.mark.parametrize("saved", [False, True], ids=["cardless", "saved-card"])
@pytest.mark.parametrize("start,end", [
    (B184_START, B184_END),
    (datetime(2097, 1, 31, 12, 30, 45, tzinfo=timezone.utc), datetime(2097, 2, 28, 12, 30, 45, tzinfo=timezone.utc)),
    (datetime(2096, 12, 31, 12, 30, 45, tzinfo=timezone.utc), datetime(2097, 1, 31, 12, 30, 45, tzinfo=timezone.utc)),
], ids=["leap-jan31", "jan31", "year-rollover"])
async def test_tester_w1_gateway_calendar(fresh_client, session_factory, tester_admin, tester_sdk, saved, start, end):
    sdk = tester_sdk
    sdk.now = start
    sdk.promo["expires_at"] = int((start + timedelta(days=5)).timestamp())
    if saved:
        async with session_factory() as session:
            await _set_customer(session, tester_admin["org_id"], sdk.saved_customer())
    response = await _redeem(fresh_client, tester_admin, "  b184-tEsTeR  ")
    _tester_success(response, end)
    row = await _tester_applied(session_factory, tester_admin, sdk, end)
    assert row.started_at == start
    [wire] = sdk.creates
    assert wire["items"] == [{"price": B184_PRICE, "quantity": 1}], "paid-price"
    assert wire["collection_method"] == "send_invoice" and wire["days_until_due"] == 1, "automatic-collection"
    assert wire["payment_settings"]["save_default_payment_method"] == "off", "attach-card"
    assert wire["cancel_at"] == int(end.timestamp()), "omit-cancel-at / thirty-day-month"
    assert wire["discounts"] == [{"promotion_code": "promo_b184"}], "omit-discount"
    assert wire["metadata"]["tester_redemption_id"] == str(row.id)
    assert isinstance(wire["idempotency_key"], str) and wire["idempotency_key"]
    assert not any(wire.get(k) for k in ("default_payment_method", "default_source", "add_invoice_items", "trial_end", "trial_period_days"))
    assert not wire.get("automatic_tax", {}).get("enabled")
    assert not [c for c in sdk.calls if c[0] == "checkout.create"]
    assert B184_PRICE not in sdk.settings.STRIPE_PRICE_MAP


@pytest.mark.parametrize("case,code", [
    ("unknown", "invalid_tester_code"), ("inactive", "invalid_tester_code"), ("untagged", "invalid_tester_code"),
    ("missing-expiry", "invalid_tester_code"), ("expired", "tester_code_expired"), ("equal-expiry", "tester_code_expired"),
    ("missing-cap", "invalid_tester_code"), ("exhausted", "tester_code_exhausted"), ("zero-cap", "invalid_tester_code"),
    ("bad-cap", "invalid_tester_code"), ("boolean-cap", "invalid_tester_code"),
    ("bad-count", "invalid_tester_code"), ("missing-count", "invalid_tester_code"),
    ("boolean-count", "invalid_tester_code"), ("bad-expiry", "invalid_tester_code"),
    ("invalid-coupon", "invalid_tester_code"), ("wrong-customer", "invalid_tester_code"),
    ("wrong-product", "invalid_tester_code"), ("minimum-spend", "invalid_tester_code"), ("ambiguous", "invalid_tester_code"),
], ids=lambda v: v)
async def test_tester_w2_code_refusals(fresh_client, session_factory, tester_admin, tester_sdk, case, code):
    sdk, p = tester_sdk, tester_sdk.promo
    if case == "unknown": sdk.promos = []
    elif case == "inactive": p["active"] = False
    elif case == "untagged": p["metadata"] = {}
    elif case == "missing-expiry": p["expires_at"] = None
    elif case == "expired": p["expires_at"] = int(sdk.now.timestamp()) - 1
    elif case == "equal-expiry": p["expires_at"] = int(sdk.now.timestamp())
    elif case == "missing-cap": p["max_redemptions"] = None
    elif case == "exhausted": p["times_redeemed"] = p["max_redemptions"]
    elif case == "zero-cap": p["max_redemptions"] = 0
    elif case == "bad-cap": p["max_redemptions"] = "many"
    elif case == "boolean-cap": p["max_redemptions"] = True
    elif case == "bad-count": p["times_redeemed"] = -1
    elif case == "missing-count": p.pop("times_redeemed")
    elif case == "boolean-count": p["times_redeemed"] = False
    elif case == "bad-expiry": p["expires_at"] = "tomorrow"
    elif case == "invalid-coupon": p["coupon"]["valid"] = False
    elif case == "wrong-customer": p["customer"] = "cus_foreign"
    elif case == "wrong-product": p["coupon"]["applies_to"] = {"products": ["prod_other"]}
    elif case == "minimum-spend": p["restrictions"].update(minimum_amount=100, minimum_amount_currency="gbp")
    elif case == "ambiguous": sdk.promos.append({**copy.deepcopy(p), "id": "promo_second"})
    _tester_refusal(await _redeem(fresh_client, tester_admin), 422, code)
    await _tester_no_effect(session_factory, sdk)


@pytest.mark.parametrize("bad", [
    {"active": False}, {"currency": "usd"}, {"unit_amount": 1, "unit_amount_decimal": "1"},
    {"unit_amount": None, "unit_amount_decimal": None}, {"unit_amount": 0, "unit_amount_decimal": "0.5"},
    {"recurring": None}, {"recurring": {"interval": "year", "interval_count": 1, "usage_type": "licensed"}},
    {"recurring": {"interval": "month", "interval_count": 2, "usage_type": "licensed"}},
    {"recurring": {"interval": "month", "interval_count": 1, "usage_type": "metered"}},
    {"billing_scheme": "tiered", "unit_amount": None}, {"id": "price_other"},
])
async def test_tester_w2_bad_price(fresh_client, session_factory, tester_admin, tester_sdk, bad):
    tester_sdk.price.update(bad)
    response = await _redeem(fresh_client, tester_admin)
    assert response.status_code in (422, 502), response.text  # configuration refusal, never activation_failed
    assert response.json().get("error", {}).get("code") != "activation_failed"
    await _tester_no_effect(session_factory, tester_sdk)


@pytest.mark.parametrize("disabled", ["", "overlap"])
async def test_tester_w2_disabled_or_overlapping_config(fresh_client, session_factory, tester_admin, tester_sdk, disabled):
    if disabled == "overlap":
        tester_sdk.settings.STRIPE_PRICE_MAP[B184_PRICE] = "devgenie:pro"
    else:
        tester_sdk.settings.STRIPE_TESTER_PRICE_ID = ""
    response = await _redeem(fresh_client, tester_admin)
    assert response.status_code in (422, 502), response.text
    await _tester_no_effect(session_factory, tester_sdk)


@pytest.mark.parametrize("boundary", ["repeat", "after-end", "other-org", "other-admin"])
async def test_tester_w3_one_use(fresh_client, session_factory, sent_otps, tester_admin, tester_sdk, boundary):
    _tester_success(await _redeem(fresh_client, tester_admin))
    actor = tester_admin
    if boundary == "after-end":
        tester_sdk.now = B184_END
    elif boundary == "other-org":
        async with session_factory() as session:
            org = Organization(name="Second", slug="b184-second")
            session.add(org)
            await session.flush()
            session.add(Membership(user_id=uuid.UUID(actor["user_id"]), org_id=org.id, role=Role.ADMIN))
            await session.commit()
            oid = str(org.id)
        login = await fresh_client.post("/api/v1/login", json={"email": "b184@example.com", "password": PASSWORD, "org_id": oid})
        assert login.status_code == 200, login.text
        actor = {**actor, "org_id": oid, "headers": {"Authorization": "Bearer " + login.json()["access_token"]}}
    elif boundary == "other-admin":
        actor = await _signup(fresh_client, sent_otps, "b184-second-admin@example.com")
        async with session_factory() as session:
            session.add(Membership(user_id=actor["user_id"], org_id=tester_admin["org_id"], role=Role.ADMIN))
            await session.commit()
        login = await fresh_client.post("/api/v1/login", json={
            "email": "b184-second-admin@example.com", "password": PASSWORD, "org_id": tester_admin["org_id"]})
        assert login.status_code == 200, login.text
        actor = {**actor, "org_id": tester_admin["org_id"], "headers": {"Authorization": "Bearer " + login.json()["access_token"]}}
    _tester_refusal(await _redeem(fresh_client, actor), 409, "tester_already_used")
    assert len(tester_sdk.creates) == 1
    assert len(await _tester_rows(session_factory)) == 1


@pytest.mark.parametrize("source", ["manual", "stripe"])
async def test_tester_w3_local_plan(fresh_client, session_factory, tester_admin, tester_sdk, source):
    async with session_factory() as session:
        plan = (await session.execute(select(Plan).where(Plan.product == "devgenie", Plan.code == "pro"))).scalar_one()
        session.add(Subscription(org_id=tester_admin["org_id"], product="devgenie", plan_id=plan.id,
                                 status="active", source=source, current_period_end=B184_END,
                                 stripe_subscription_id="sub_paid" if source == "stripe" else None))
        await session.commit()
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "subscription_exists")
    assert tester_sdk.creates == []
    assert not [r for r in await _tester_rows(session_factory) if r.state == "applied"]


@pytest.mark.parametrize("live", ["trialing", "active", "past_due", "unpaid", "incomplete", "paused"])
@pytest.mark.parametrize("price", ["price_pro", B184_PRICE], ids=["paid", "tester"])
async def test_tester_w3_stripe_live(fresh_client, session_factory, tester_admin, tester_sdk, live, price):
    cid = tester_sdk.saved_customer()
    async with session_factory() as session:
        await _set_customer(session, tester_admin["org_id"], cid)
    bundle = _two_item_sub("sub_bundle", status=live, dev_price=price)
    bundle["customer"] = cid
    tester_sdk.subscriptions["sub_bundle"] = bundle
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "subscription_exists")
    await _tester_no_effect(session_factory, tester_sdk)


async def test_tester_w3_open_checkout(fresh_client, session_factory, tester_admin, tester_sdk):
    cid = tester_sdk.saved_customer()
    async with session_factory() as session:
        await _set_customer(session, tester_admin["org_id"], cid)
    tester_sdk.open_sessions.append({"id": "cs_paid", "customer": cid, "url": "https://checkout.stripe.test/open",
                                     "metadata": {"price_id": "price_ent", "org_id": tester_admin["org_id"]}})
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "subscription_exists")
    reads = [c[0] for c in tester_sdk.calls if c[0] in ("checkout.list", "subscription.list")]
    assert reads and reads[0] == "checkout.list"
    await _tester_no_effect(session_factory, tester_sdk)


async def test_tester_w3_member_exact_reply(fresh_client, session_factory, sent_otps, tester_admin, tester_sdk):
    # Real member credentials: do NOT override auth dependencies or grant billing:write.
    async with session_factory() as session:
        member = await _add_member(fresh_client, session, sent_otps, tester_admin["org_id"], "b184-member@example.com")
    member["org_id"] = tester_admin["org_id"]
    tester_sdk.calls.clear()
    response = await _redeem(fresh_client, member)
    _tester_capture("member", response)
    assert tester_sdk.calls == [] and tester_sdk.creates == []


async def test_tester_w3_admin_without_billing_scope(fresh_client, tester_admin, tester_sdk):
    token = create_access_token(tester_sdk.settings, tester_admin["user_id"], "b184@example.com",
                                "admin", tester_admin["org_id"], ["entitlements:read"])[0]
    actor = {**tester_admin, "headers": {"Authorization": "Bearer " + token}}
    tester_sdk.calls.clear()
    response = await _redeem(fresh_client, actor)
    assert response.status_code == 403, response.text
    assert tester_sdk.calls == [] and tester_sdk.creates == []


@pytest.mark.parametrize("kind", ["missing", "bad", "foreign"])
async def test_tester_w3_auth_boundary(fresh_client, session_factory, sent_otps, tester_admin, tester_sdk, kind):
    if kind == "foreign":
        other = await _signup(fresh_client, sent_otps, "b184-foreign@example.com")
        headers = other["headers"]
    else:
        headers = {} if kind == "missing" else {"Authorization": "Bearer forged"}
    response = await fresh_client.post(_tester_url(tester_admin), headers=headers, json={"code": B184_CODE})
    assert response.status_code == (403 if kind == "foreign" else 401), response.text
    assert tester_sdk.calls == []
    await _tester_no_effect(session_factory, tester_sdk)


@pytest.mark.parametrize("body", [
    {}, {"code": ""}, {"code": "   "}, {"code": "x" * 65}, {"code": 1}, {"code": None},
    {"code": ["one", "two"]}, {"code": B184_CODE, "org_id": "foreign"},
    {"code": B184_CODE, "user_id": "foreign"}, {"code": B184_CODE, "price_id": "price_pro"},
    {"code": B184_CODE, "product": "other"}, {"code": B184_CODE, "payment_method": "pm_saved"},
    {"code": B184_CODE, "tier": "enterprise"}, [], None, "not an object",
])
async def test_tester_w3_closed_body(fresh_client, session_factory, tester_admin, tester_sdk, body):
    response = await fresh_client.post(_tester_url(tester_admin), headers=tester_admin["headers"], json=body)
    _tester_refusal(response, 422, "invalid_tester_code")
    await _tester_no_effect(session_factory, tester_sdk)


async def _tester_pending(client, actor, factory, sdk, outcome):
    sdk.fail = outcome
    response = await _redeem(client, actor)
    assert response.status_code == 502, response.text
    [pending] = await _tester_rows(factory)
    assert pending.state == "pending" and pending.ends_at == B184_END, "forget-pending"
    async with factory() as session:
        assert list((await session.execute(select(Subscription))).scalars()) == []
        ent = await EntitlementService(session, sdk.settings, AuditService(session, sdk.settings)).resolve(actor["org_id"], "devgenie")
        assert ent.tier == "lite"
        org = await session.get(Organization, uuid.UUID(actor["org_id"]))
        assert org.stripe_customer_id == pending.stripe_customer_id
    return pending


async def test_tester_w4_retry_identical_payload(fresh_client, session_factory, tester_admin, tester_sdk):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "before")
    original = copy.deepcopy(tester_sdk.creates[0])
    tester_sdk.now += timedelta(hours=2)
    _tester_success(await _redeem(fresh_client, tester_admin, "A-DIFFERENT-SUBMITTED-CODE"))
    assert len(tester_sdk.creates) == 2 and tester_sdk.creates[1] == original, "new-idempotency-key / refresh-retry-end"
    assert len(tester_sdk.successes) == 1
    await _tester_applied(session_factory, tester_admin, tester_sdk)


@pytest.mark.parametrize("change", ["last-slot", "code-expired", "commit-failure"])
async def test_tester_w4_live_recovery(fresh_client, session_factory, tester_admin, tester_sdk, monkeypatch, change):
    sdk = tester_sdk
    sdk.promo["max_redemptions"] = 1
    if change == "commit-failure":
        commit = AsyncSession.commit
        armed = True

        async def fail_local_commit(session):
            nonlocal armed
            if armed and sdk.successes:
                armed = False
                await session.rollback()
                raise sa.exc.OperationalError("injected tester commit failure", {}, RuntimeError("B184"))
            return await commit(session)

        monkeypatch.setattr(AsyncSession, "commit", fail_local_commit)
        # ASGITransport surfaces an unhandled DB error; an AppError/HTTP 5xx is also legitimate here.
        try:
            response = await _redeem(fresh_client, tester_admin)
        except sa.exc.OperationalError:
            pass
        else:
            assert response.status_code >= 500, response.text
        monkeypatch.setattr(AsyncSession, "commit", commit)
        [pending] = await _tester_rows(session_factory)
        assert pending.state == "pending", "reservation must commit before the external write"
    else:
        pending = await _tester_pending(fresh_client, tester_admin, session_factory, sdk, "after")
        assert pending.stripe_subscription_id is None, "lost response must exercise metadata-based recovery"
    if change == "code-expired":
        sdk.now = B184_START + timedelta(days=11)
        sdk.promo["active"] = False
    sdk.calls.clear()
    _tester_success(await _redeem(fresh_client, tester_admin, "UNRELATED-CODE"))
    assert len(sdk.creates) == 1 and len(sdk.successes) == 1, "eligibility-before-recovery"
    row = await _tester_applied(session_factory, tester_admin, sdk)
    assert row.ends_at == pending.ends_at
    assert not [c for c in sdk.calls if c[0] in ("promotion.list", "promotion.retrieve")], "recovery reran fresh-code eligibility"


@pytest.mark.parametrize("ending", ["cancelled", "elapsed", "elapsed-with-paid-replacement"])
async def test_tester_w4_terminal_recovery(fresh_client, session_factory, tester_admin, tester_sdk, ending):
    sdk = tester_sdk
    await _tester_pending(fresh_client, tester_admin, session_factory, sdk, "after")
    [sid] = sdk.successes
    if ending == "cancelled":
        sdk.subscriptions[sid]["status"] = "canceled"
    else:
        sdk.now = B184_END + timedelta(seconds=1)
        sdk.subscriptions[sid]["status"] = "canceled"
    if ending.endswith("replacement"):
        async with session_factory() as session:
            await _put_row(session, tester_admin["org_id"], "sub_paid_replacement", "active",
                           int((B184_END + timedelta(days=40)).timestamp()), False,
                           customer=sdk.subscriptions[sid]["customer"])
    _tester_refusal(await _redeem(fresh_client, tester_admin, "UNRELATED-CODE"), 409, "tester_already_used")
    [row] = await _tester_rows(session_factory)
    assert row.state == "applied" and row.stripe_subscription_id == sid, "active-only-recovery"
    assert row.ends_at == B184_END and len(sdk.creates) == 1
    async with session_factory() as session:
        subs = list((await session.execute(select(Subscription))).scalars())
        if ending.endswith("replacement"):
            assert len(subs) == 1 and subs[0].stripe_subscription_id == "sub_paid_replacement"
            assert subs[0].cancel_at_period_end is False
            return
        assert subs == [], "terminal recovery must not upsert a Subscription"
        ent = await EntitlementService(session, sdk.settings, AuditService(session, sdk.settings)).resolve(tester_admin["org_id"], "devgenie")
        assert ent.tier == "lite"
    response = await fresh_client.post(_checkout_url(tester_admin), headers=tester_admin["headers"], json={"price_id": "price_pro"})
    assert response.status_code == 200, response.text
    assert len([c for c in sdk.calls if c[0] == "checkout.create"]) == 1
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "tester_already_used")


@pytest.mark.parametrize("uncertain", ["lookup-down", "beyond-retention"])
async def test_tester_w4_uncertain_keeps_pending(fresh_client, session_factory, tester_admin, tester_sdk, uncertain):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "before")
    if uncertain == "lookup-down":
        tester_sdk.fail_lookup = True
    else:
        tester_sdk.now += timedelta(days=2)  # no confirmed success and the original Stripe retention has elapsed
    response = await _redeem(fresh_client, tester_admin)
    assert response.status_code == 502, response.text
    [pending] = await _tester_rows(session_factory)
    assert pending.state == "pending"
    assert len(tester_sdk.creates) == 1 and tester_sdk.successes == []


async def test_tester_w4_pending_blocks_paid(fresh_client, session_factory, tester_admin, tester_sdk):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "before")
    response = await fresh_client.post(_checkout_url(tester_admin), headers=tester_admin["headers"], json={"price_id": "price_pro"})
    _tester_refusal(response, 409, "subscription_exists")
    assert not [c for c in tester_sdk.calls if c[0] == "checkout.create"], "ignore-pending-tester"


@pytest.mark.parametrize("mismatch", ["customer", "price", "promo", "end", "nonzero"])
async def test_tester_w4_recovery_checks_external_identity(fresh_client, session_factory, tester_admin, tester_sdk, mismatch):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "after")
    sub = tester_sdk.subscriptions[tester_sdk.successes[0]]
    if mismatch == "customer":
        async with session_factory() as session:
            model = _tester_model()
            row = (await session.execute(select(model))).scalar_one()
            row.stripe_subscription_id = sub["id"]
            await session.commit()
    if mismatch == "customer": sub["customer"] = "cus_foreign"
    elif mismatch == "price": sub["items"]["data"][0]["price"]["id"] = "price_pro"
    elif mismatch == "promo":
        sub["discounts"][0]["promotion_code"] = "promo_foreign"
        sub["discount"]["promotion_code"] = "promo_foreign"
    elif mismatch == "end": sub["cancel_at"] += 86400
    else: sub["latest_invoice"].update(total=100, amount_due=100)
    response = await _redeem(fresh_client, tester_admin)
    assert response.status_code == 502, response.text
    [row] = await _tester_rows(session_factory)
    assert row.state == "pending" and len(tester_sdk.creates) == 1


async def test_tester_w4_active_recovery_preserves_other_plan(fresh_client, session_factory, tester_admin, tester_sdk):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "after")
    async with session_factory() as session:
        await _put_row(session, tester_admin["org_id"], "sub_other_plan", "active", int(B184_END.timestamp()), False,
                       customer=tester_sdk.subscriptions[tester_sdk.successes[0]]["customer"])
    response = await _redeem(fresh_client, tester_admin)
    assert response.status_code == 409, response.text
    async with session_factory() as session:
        row = (await session.execute(select(Subscription))).scalar_one()
        assert row.stripe_subscription_id == "sub_other_plan" and row.cancel_at_period_end is False
    assert len(tester_sdk.creates) == 1


async def test_tester_w5_entitlements_and_clock_expiry(fresh_client, session_factory, tester_admin, tester_sdk):
    _tester_success(await _redeem(fresh_client, tester_admin))
    async with session_factory() as session:
        session.add(ExternalIdentity(user_id=tester_admin["user_id"], provider=ExternalProvider.ENTRA_EXTERNAL,
                                     provider_user_id="b184-oid", email="b184@example.com"))
        await session.commit()
    for now, tier in [(B184_START, "pro"), (B184_END - timedelta(seconds=1), "pro"),
                      (B184_END, "lite"), (B184_END + timedelta(days=1), "lite")]:
        tester_sdk.now = now
        async with session_factory() as session:
            ent = await EntitlementService(session, tester_sdk.settings, AuditService(session, tester_sdk.settings)).resolve(tester_admin["org_id"], "devgenie")
            assert ent.tier == tier, "omit-local-end"
        response = await fresh_client.get("/api/v1/internal/entitlements/by-subject",
                                          params={"provider": "entra", "oid": "b184-oid", "product": "devgenie"},
                                          headers={"X-Service-Secret": B184_SECRET})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tier"] == tier and body["account_id"] == tester_admin["user_id"]
        assert body["org_id"] == tester_admin["org_id"]
        if tier == "pro":
            assert datetime.fromisoformat(body["expires_at"]) == B184_END
        else:
            assert body["expires_at"] is None
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "tester_already_used")


@pytest.mark.parametrize("cancel_at", ["absent", "extended"])
@pytest.mark.parametrize("flag", [False, None], ids=["false-ending-flag", "missing-ending-flag"])
async def test_tester_w5_authoritative_deadline(fresh_client, session_factory, tester_admin, tester_sdk, cancel_at, flag):
    cid = tester_sdk.saved_customer()
    async with session_factory() as session:
        await _set_customer(session, tester_admin["org_id"], cid)
    _tester_success(await _redeem(fresh_client, tester_admin))
    [sid] = tester_sdk.successes
    current = tester_sdk.subscriptions[sid]
    current["current_period_end"] = current["items"]["data"][0]["current_period_end"] = int((B184_END + timedelta(days=60)).timestamp())
    if cancel_at == "absent": current.pop("cancel_at")
    else: current["cancel_at"] = int((B184_END + timedelta(days=60)).timestamp())
    if flag is None: current.pop("cancel_at_period_end")
    else: current["cancel_at_period_end"] = flag
    event = _updated_event("evt_b184_clamp", sid, current_period_end=int((B184_END + timedelta(days=90)).timestamp()),
                           cancel_at_period_end=False, cancel_at=int((B184_END + timedelta(days=90)).timestamp()))
    assert (await _post(fresh_client, event)).status_code == 200
    await _tester_applied(session_factory, tester_admin, tester_sdk)
    response = await fresh_client.get(f"/api/v1/orgs/{tester_admin['org_id']}/billing", headers=tester_admin["headers"])
    _tester_capture("billing", response)  # exact actual HTTP reply replayed by W7
    assert (await _post(fresh_client, event)).status_code == 200
    async with session_factory() as session:
        assert len(list((await session.execute(select(StripeEvent).where(StripeEvent.event_id == event["id"]))).scalars())) == 1


async def test_tester_w5_cancel_stale_events_and_paid_replacement(fresh_client, session_factory, tester_admin, tester_sdk):
    _tester_success(await _redeem(fresh_client, tester_admin))
    [sid] = tester_sdk.successes
    tester_sdk.subscriptions[sid]["status"] = "canceled"
    deleted = _deleted_event("evt_b184_deleted", sid, status="active")
    stale = _updated_event("evt_b184_stale", sid, status="active",
                           current_period_end=int((B184_END + timedelta(days=60)).timestamp()))
    for event in [deleted, deleted, stale]:
        assert (await _post(fresh_client, event)).status_code == 200
    async with session_factory() as session:
        ent = await EntitlementService(session, tester_sdk.settings, AuditService(session, tester_sdk.settings)).resolve(tester_admin["org_id"], "devgenie")
        assert ent.tier == "lite", "accept-event-snapshot"
        row = (await session.execute(select(Subscription))).scalar_one()
        assert row.status == "cancelled" and row.current_period_end == B184_END
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "tester_already_used")
    assert len(await _tester_rows(session_factory)) == 1, "delete-redemption-on-cancel"
    paid_id = "sub_evt_b184_paid"
    paid_end = int((B184_END + timedelta(days=60)).timestamp())
    customer = tester_sdk.subscriptions[sid]["customer"]
    tester_sdk.subscriptions[paid_id] = {
        "id": paid_id, "customer": customer, "status": "active", "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": "price_pro"}, "current_period_end": paid_end}]},
    }
    event = _checkout_event("evt_b184_paid", tester_admin["org_id"], customer=customer, subscription=paid_id)
    assert (await _post(fresh_client, event)).status_code == 200
    # Whether a superseded tester event is a recorded no-op or retryable unknown, it must not change the paid row.
    await _post(fresh_client, _updated_event("evt_b184_old_again", sid, status="active", cancel_at_period_end=True))
    async with session_factory() as session:
        row = (await session.execute(select(Subscription))).scalar_one()
        assert row.stripe_subscription_id == paid_id and row.cancel_at_period_end is False
        assert _epoch(row.current_period_end) == paid_end
    _tester_refusal(await _redeem(fresh_client, tester_admin), 409, "tester_already_used")


async def test_tester_w5_event_before_local_commit_retries(fresh_client, session_factory, tester_admin, tester_sdk):
    await _tester_pending(fresh_client, tester_admin, session_factory, tester_sdk, "after")
    [sid] = tester_sdk.successes
    event = _updated_event("evt_b184_early", sid, status="active")
    early = await _post(fresh_client, event)
    assert early.status_code == 500, early.text
    async with session_factory() as session:
        assert await session.get(StripeEvent, event["id"]) is None
    _tester_success(await _redeem(fresh_client, tester_admin))
    assert (await _post(fresh_client, event)).status_code == 200
    await _tester_applied(session_factory, tester_admin, tester_sdk)


def test_tester_w6_setting_and_migration():
    from app.core.config import Settings
    assert "STRIPE_TESTER_PRICE_ID" in Settings.model_fields, "tester setting is absent"
    assert Settings.model_fields["STRIPE_TESTER_PRICE_ID"].default in ("", None)
    cfg = Config(str(B184_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(B184_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["20261113_000008"], "omit-redemption-migration"
    assert script.get_revision("20261113_000008").down_revision == "20261113_000007"
    assert script.get_revision("20261113_000007").down_revision == "20261021_000006"
    model = _tester_model()
    path = B184_ROOT / "alembic/versions/20261113_000008_tester_redemptions.py"
    assert path.is_file(), "omit-redemption-migration"
    spec = importlib.util.spec_from_file_location("b184_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    fields = {"id", "user_id", "org_id", "promotion_code_id", "tester_price_id", "stripe_customer_id",
              "stripe_subscription_id", "started_at", "ends_at", "state"}
    assert set(model.__table__.c.keys()) == fields, "no code, token or card data belongs in this table"
    from app.db.types import UUIDType, TZDateTime
    assert all(isinstance(model.__table__.c[n].type, UUIDType) for n in ("id", "user_id", "org_id"))
    assert all(isinstance(model.__table__.c[n].type, TZDateTime) for n in ("started_at", "ends_at"))
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=[t for t in Base.metadata.sorted_tables if t is not model.__table__])
            before = set(sa.inspect(conn).get_table_names())
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()
            inspector = sa.inspect(conn)
            assert set(inspector.get_table_names()) == before | {model.__tablename__}
            cols = {c["name"]: c for c in inspector.get_columns(model.__tablename__)}
            assert set(cols) == fields
            for name, column in model.__table__.c.items():
                assert cols[name]["nullable"] == column.nullable
                assert str(cols[name]["type"]) == str(column.type.compile(dialect=conn.dialect))
            uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(model.__tablename__)}
            uniques |= {tuple(i["column_names"]) for i in inspector.get_indexes(model.__tablename__) if i["unique"]}
            assert {("user_id",), ("org_id",), ("stripe_subscription_id",)} <= uniques, "drop-user-unique"
            expected_unique = {tuple(c.name for c in u.columns) for u in model.__table__.constraints if isinstance(u, sa.UniqueConstraint)}
            expected_unique |= {tuple(c.name for c in i.columns) for i in model.__table__.indexes if i.unique}
            assert uniques == expected_unique
            migrated_fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                            for f in inspector.get_foreign_keys(model.__tablename__)}
            model_fks = {(tuple(e.parent.name for e in f.elements), f.elements[0].column.table.name,
                          tuple(e.column.name for e in f.elements)) for f in model.__table__.foreign_key_constraints}
            assert migrated_fks == model_fks
            assert inspector.get_pk_constraint(model.__tablename__)["constrained_columns"] == ["id"]
        # Execute uniqueness violations against the MIGRATED schema, not just metadata inspection.
        base = dict(id=uuid.uuid4(), user_id=uuid.uuid4(), org_id=uuid.uuid4(),
                    promotion_code_id="promo_b184", tester_price_id=B184_PRICE, stripe_customer_id="cus_b184",
                    stripe_subscription_id="sub_unique", started_at=B184_START, ends_at=B184_END, state="applied")
        with engine.begin() as conn:
            conn.execute(model.__table__.insert().values(**base))
        for field in ("user_id", "org_id", "stripe_subscription_id"):
            row = {**base, "id": uuid.uuid4(), "user_id": uuid.uuid4(), "org_id": uuid.uuid4(),
                   "stripe_subscription_id": "sub_" + uuid.uuid4().hex}
            row[field] = base[field]
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(model.__table__.insert().values(**row))
        with engine.begin() as conn:
            for _ in range(2):
                conn.execute(model.__table__.insert().values(**{
                    **base, "id": uuid.uuid4(), "user_id": uuid.uuid4(), "org_id": uuid.uuid4(),
                    "stripe_subscription_id": None, "state": "pending"}))
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
            assert set(sa.inspect(conn).get_table_names()) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("overlap", [False, True])
async def test_tester_w6_reject_tester_checkout(fresh_client, session_factory, tester_admin, tester_sdk, overlap):
    if overlap:
        tester_sdk.settings.STRIPE_PRICE_MAP[B184_PRICE] = "devgenie:pro"
    response = await fresh_client.post(_checkout_url(tester_admin), headers=tester_admin["headers"], json={"price_id": B184_PRICE})
    assert response.status_code == 422, response.text
    assert not [c for c in tester_sdk.calls if c[0] == "checkout.create"], "accept-tester-checkout"
    await _tester_no_effect(session_factory, tester_sdk)


@pytest.mark.parametrize("payment_status", ["paid", "no_payment_required"])
async def test_tester_w6_paid_checkout_unchanged(fresh_client, session_factory, tester_admin, tester_sdk, payment_status):
    response = await fresh_client.post(_checkout_url(tester_admin), headers=tester_admin["headers"], json={"price_id": "price_pro"})
    assert response.status_code == 200, response.text
    [wire] = [c[1] for c in tester_sdk.calls if c[0] == "checkout.create"]
    assert wire["line_items"] == [{"price": "price_pro", "quantity": 1}]
    assert wire["allow_promotion_codes"] is True and wire["payment_method_types"] == ["card"]
    assert "payment_method_collection" not in wire
    sid = "sub_evt_b184_ordinary"
    tester_sdk.subscriptions[sid] = {
        "id": sid, "customer": wire["customer"], "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}, "current_period_end": int(B184_END.timestamp())}]},
        "cancel_at_period_end": False,
    }
    event = _checkout_event("evt_b184_ordinary", tester_admin["org_id"], customer=wire["customer"], payment_status=payment_status)
    assert (await _post(fresh_client, event)).status_code == 200
    async with session_factory() as session:
        ent = await EntitlementService(session, tester_sdk.settings, AuditService(session, tester_sdk.settings)).resolve(tester_admin["org_id"], "devgenie")
        assert ent.tier == "pro"
    assert await _tester_rows(session_factory) == []


@pytest.fixture
async def tester_postgres_db(fake_redis):
    """Real independent sessions in a disposable random schema; never uses DATABASE_URL.

    Missing B184_TEST_DATABASE_URL skips only these explicit PostgreSQL witnesses. A skip is NOT acceptance.
    The supplied database must be disposable. Only this fixture's random schema is dropped.
    """
    url = os.environ.get("B184_TEST_DATABASE_URL")
    if not url:
        pytest.skip("W4 requires B184_TEST_DATABASE_URL to a disposable PostgreSQL DB; skip is not locking evidence")
    assert url.startswith("postgresql+asyncpg://"), "B184_TEST_DATABASE_URL must use postgresql+asyncpg"
    schema = "b184_" + uuid.uuid4().hex
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sessions = []
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(Plan(**plan) for plan in DEFAULT_PLANS)
            await session.commit()

        async def per_request():
            async with factory() as session:
                sessions.append(session)
                yield session

        app.dependency_overrides[get_session] = per_request
        app.state.redis = fake_redis
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, engine, sessions
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.state.redis = None
        async with engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.parametrize("race", ["same-account", "last-slot", "same-user-two-orgs", "two-admins-one-org"])
async def test_tester_postgres_w4_races(tester_postgres_db, tester_sdk, sent_otps, race):
    client, factory, engine, sessions = tester_postgres_db
    a = await _signup(client, sent_otps, "b184-pga@example.com")
    b = a
    if race in ("last-slot", "two-admins-one-org"):
        b = await _signup(client, sent_otps, "b184-pgb@example.com")
    if race == "last-slot":
        tester_sdk.promo["max_redemptions"] = 1
    if race in ("same-user-two-orgs", "two-admins-one-org"):
        async with factory() as session:
            if race == "same-user-two-orgs":
                org = Organization(name="Race second org", slug="b184-race-other")
                session.add(org)
                await session.flush()
                oid = str(org.id)
            else:
                oid = a["org_id"]
            session.add(Membership(user_id=b["user_id"], org_id=oid, role=Role.ADMIN))
            await session.commit()
        login = await client.post("/api/v1/login", json={
            "email": "b184-pga@example.com" if race == "same-user-two-orgs" else "b184-pgb@example.com",
            "password": PASSWORD, "org_id": oid})
        assert login.status_code == 200, login.text
        b = {**b, "org_id": oid, "headers": {"Authorization": "Bearer " + login.json()["access_token"]}}
    before = len(sessions)
    responses = await asyncio.wait_for(asyncio.gather(_redeem(client, a), _redeem(client, b)), 15)
    assert len(sessions) == before + 2 and sessions[-1] is not sessions[-2]
    assert sorted(r.status_code for r in responses)[0] == 200, [r.text for r in responses]
    if race != "same-account":
        assert sum(r.status_code == 200 for r in responses) == 1, [r.text for r in responses]
    assert all(r.status_code in (200, 409, 422) for r in responses), [r.text for r in responses]
    if race == "last-slot":
        assert [r.json()["error"]["code"] for r in responses if r.status_code != 200] == ["tester_code_exhausted"]
    else:
        assert all(r.json()["error"]["code"] == "tester_already_used" for r in responses if r.status_code != 200)
    assert len(tester_sdk.successes) == 1
    if race != "last-slot":
        assert len(tester_sdk.creates) == 1
    assert len([r for r in await _tester_rows(factory) if r.state == "applied"]) == 1


async def test_tester_postgres_w4_org_lock_and_pending_checkout(tester_postgres_db, tester_sdk, sent_otps):
    client, factory, engine, _ = tester_postgres_db
    actor = await _signup(client, sent_otps, "b184-lock@example.com")
    # Preseed customer: incidental ensure_customer UPDATE must not impersonate an explicit lock.
    async with factory() as session:
        await _set_customer(session, actor["org_id"], tester_sdk.saved_customer())
    tester_sdk.fail = "before"
    # NO KEY UPDATE blocks the explicit FOR UPDATE but permits the reservation FK KEY SHARE check.
    async with factory() as blocker:
        await blocker.execute(select(Organization).where(Organization.id == uuid.UUID(actor["org_id"])).with_for_update(key_share=True))
        pending = asyncio.create_task(_redeem(client, actor))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), .25)
            assert tester_sdk.creates == [], "no-org-lock: external write ran through a held org lock"
        finally:
            await blocker.rollback()
        response = await asyncio.wait_for(pending, 15)
    assert response.status_code == 502, response.text
    [row] = await _tester_rows(factory)
    assert row.state == "pending"
    # Paid purchase and pending-retry have distinct sessions and run concurrently.
    tester_sdk.fail_lookup = True
    paid, retry = await asyncio.wait_for(asyncio.gather(
        client.post(_checkout_url(actor), headers=actor["headers"], json={"price_id": "price_pro"}),
        _redeem(client, actor),
    ), 15)
    _tester_refusal(paid, 409, "subscription_exists")
    assert retry.status_code == 502
    assert not [c for c in tester_sdk.calls if c[0] == "checkout.create"], "ignore-pending-tester"
