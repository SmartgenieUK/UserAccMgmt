"""BRIEF-029 (CR-214): UAM keeps a subscription's period end and a pending cancellation, the price map reads its
documented env form, and the billing portal can open straight at Stripe's cancel confirmation.

The fixture is built from Stripe's documented 2026-03-25 (dahlia) subscription shape, where current_period_end
lives on the subscription items, not on the subscription. It is not yet a captured sandbox event; co-arch pins it
against a real one at acceptance.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.main import app
from app.services.stripe_gateway import get_stripe_gateway
from tests.test_billing import FakeStripeGateway, _checkout_event, _post, admin, billing_settings  # noqa: F401

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "stripe_subscription_updated_2026-03-25.json").read_text())
PERIOD_END = datetime.fromtimestamp(FIXTURE["data"]["object"]["items"]["data"][0]["current_period_end"], tz=timezone.utc)


def _updated(event_id, sub_id, **fields):
    """The fixture event, re-addressed to one subscription, with top-level subscription fields overridden."""
    event = copy.deepcopy(FIXTURE)
    event["id"] = event_id
    obj = event["data"]["object"]
    obj["id"] = sub_id
    obj.update(fields)
    return event


def _item_period_end(event, ts):
    event["data"]["object"]["items"]["data"][0]["current_period_end"] = ts
    return event


async def _activate(client, admin, event_id):
    assert (await _post(client, _checkout_event(event_id, admin["org_id"]))).status_code == 200
    return "sub_" + event_id


async def _entitlement(client, admin):
    res = await client.get("/api/v1/entitlements/me?product=devgenie", headers=admin["headers"])
    assert res.status_code == 200, res.text
    return res.json()


async def _billing(client, admin):
    res = await client.get(f"/api/v1/orgs/{admin['org_id']}/billing", headers=admin["headers"])
    assert res.status_code == 200, res.text
    return res.json()["subscriptions"][0]


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_a_new_subscription_is_not_ending(client, db_session, admin, billing_settings):
    await _activate(client, admin, "evt_p1")
    ent = await _entitlement(client, admin)
    assert ent["tier"] == "pro"
    assert ent["cancel_at_period_end"] is False
    assert (await _billing(client, admin))["cancel_at_period_end"] is False


@pytest.mark.asyncio
async def test_the_period_end_is_read_from_the_subscription_item(client, db_session, admin, billing_settings):
    sub = await _activate(client, admin, "evt_p2")
    assert (await _post(client, _updated("evt_p2_upd", sub, cancel_at_period_end=False))).status_code == 200
    ent = await _entitlement(client, admin)
    assert ent["tier"] == "pro"
    assert _ts(ent["expires_at"]) == PERIOD_END
    assert _ts((await _billing(client, admin))["current_period_end"]) == PERIOD_END


@pytest.mark.asyncio
async def test_the_top_level_period_end_is_the_fallback(client, db_session, admin, billing_settings):
    sub = await _activate(client, admin, "evt_p3")
    event = _updated("evt_p3_upd", sub, cancel_at_period_end=False, current_period_end=int(PERIOD_END.timestamp()))
    del event["data"]["object"]["items"]["data"][0]["current_period_end"]
    assert (await _post(client, event)).status_code == 200
    assert _ts((await _entitlement(client, admin))["expires_at"]) == PERIOD_END


@pytest.mark.asyncio
async def test_a_renewal_moves_the_period_end_so_a_paying_customer_stays_pro(client, db_session, admin, billing_settings):
    sub = await _activate(client, admin, "evt_p4")
    past = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    assert (await _post(client, _item_period_end(_updated("evt_p4_a", sub, cancel_at_period_end=False), past))).status_code == 200
    assert (await _entitlement(client, admin))["tier"] == "lite"  # a lapsed period does not entitle
    assert (await _post(client, _updated("evt_p4_b", sub, cancel_at_period_end=False))).status_code == 200
    ent = await _entitlement(client, admin)
    assert ent["tier"] == "pro"
    assert _ts(ent["expires_at"]) == PERIOD_END


@pytest.mark.asyncio
async def test_cancel_at_period_end_is_stored_shown_and_can_be_undone(client, db_session, admin, billing_settings):
    sub = await _activate(client, admin, "evt_p5")
    assert (await _post(client, _updated("evt_p5_cancel", sub))).status_code == 200  # the fixture: pending cancel
    ent = await _entitlement(client, admin)
    assert ent["tier"] == "pro" and ent["status"] == "active"  # still paid until the period ends
    assert ent["cancel_at_period_end"] is True
    assert (await _billing(client, admin))["cancel_at_period_end"] is True
    assert (await _post(client, _updated("evt_p5_resume", sub, cancel_at_period_end=False))).status_code == 200
    assert (await _entitlement(client, admin))["cancel_at_period_end"] is False


def test_the_price_map_reads_its_documented_env_form(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_MAP", "price_x=devgenie:pro, price_y=devgenie:enterprise")
    assert Settings(_env_file=None).STRIPE_PRICE_MAP == {"price_x": "devgenie:pro", "price_y": "devgenie:enterprise"}


def test_the_price_map_still_reads_json_from_the_env(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_MAP", '{"price_x": "devgenie:pro"}')
    assert Settings(_env_file=None).STRIPE_PRICE_MAP == {"price_x": "devgenie:pro"}


def test_a_malformed_price_map_entry_fails_loud(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_MAP", "price_x")
    with pytest.raises(ValueError, match="priceId=product:tier"):
        Settings(_env_file=None)


@pytest.fixture
def gateway(billing_settings):
    from app.core.config import get_settings

    fake = FakeStripeGateway(get_settings())
    app.dependency_overrides[get_stripe_gateway] = lambda: fake
    return fake


def _portal_url(admin):
    return f"/api/v1/orgs/{admin['org_id']}/billing/portal-session"


@pytest.mark.asyncio
async def test_the_cancel_flow_opens_stripes_cancel_confirmation_for_the_orgs_subscription(client, db_session, admin, gateway):
    sub = await _activate(client, admin, "evt_p6")
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel", "product": "devgenie"})
    assert res.status_code == 200, res.text
    assert gateway.calls[-1] == ("create_portal_session", "cus_test123", sub)


@pytest.mark.asyncio
async def test_without_a_flow_the_portal_session_is_unchanged(client, db_session, admin, gateway):
    await _activate(client, admin, "evt_p7")
    res = await client.post(_portal_url(admin), headers=admin["headers"])
    assert res.status_code == 200, res.text
    assert gateway.calls[-1] == ("create_portal_session", "cus_test123", None)


@pytest.mark.asyncio
async def test_the_cancel_flow_without_a_subscription_is_refused(client, db_session, admin, gateway):
    assert (await client.post(f"/api/v1/orgs/{admin['org_id']}/billing/checkout-session", headers=admin["headers"],
                              json={"price_id": "price_pro"})).status_code == 200  # a customer, no subscription yet
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel", "product": "devgenie"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "no_subscription"


@pytest.mark.asyncio
async def test_an_unknown_flow_is_refused(client, db_session, admin, gateway):
    await _activate(client, admin, "evt_p8")
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "upgrade"})
    assert res.status_code == 422
    assert len([c for c in gateway.calls if c[0] == "create_portal_session"]) == 0


def test_the_gateway_sends_stripe_the_cancel_flow(monkeypatch):
    import stripe
    from app.core.config import get_settings
    from app.services.stripe_gateway import StripeGateway

    sent = {}

    class _Session:
        url = "https://portal.stripe.test/cancel"

    monkeypatch.setattr(stripe.billing_portal.Session, "create", lambda **kw: sent.update(kw) or _Session())
    url = StripeGateway(get_settings()).create_portal_session("cus_1", "https://back.test", cancel_subscription_id="sub_1")
    assert url == "https://portal.stripe.test/cancel"
    assert sent["customer"] == "cus_1" and sent["return_url"] == "https://back.test"
    assert sent["flow_data"] == {
        "type": "subscription_cancel",
        "subscription_cancel": {"subscription": "sub_1"},
        "after_completion": {"type": "redirect", "redirect": {"return_url": "https://back.test"}},
    }


@pytest.mark.asyncio
async def test_the_cancel_flow_needs_a_product(client, db_session, admin, gateway):
    await _activate(client, admin, "evt_p9")
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel"})
    assert res.status_code == 422
    assert not [c for c in gateway.calls if c[0] == "create_portal_session"]


@pytest.mark.asyncio
async def test_the_cancel_flow_picks_this_products_live_subscription(client, db_session, admin, gateway):
    # UAM is shared: another product's subscription and an old cancelled one must never be the one cancelled.
    import uuid

    from sqlalchemy import select

    from app.models import Organization, Plan, Subscription

    org_id = uuid.UUID(admin["org_id"])
    org = await db_session.get(Organization, org_id)
    org.stripe_customer_id = "cus_test123"
    pro = (await db_session.execute(select(Plan).where(Plan.product == "devgenie", Plan.code == "pro"))).scalar_one()
    other = Organization(name="Old", slug=f"old-{admin['org_id'][:8]}")
    db_session.add(other)
    await db_session.flush()
    db_session.add_all([
        # a different product in the same org (its plan row is borrowed; only product + status matter here)
        Subscription(org_id=org_id, product="cloudgenie", plan_id=pro.id, status="active", source="stripe",
                     stripe_customer_id="cus_test123", stripe_subscription_id="sub_cloudgenie"),
        # an old, cancelled devgenie subscription (in another org row, since (org, product) is unique)
        Subscription(org_id=other.id, product="devgenie", plan_id=pro.id, status="cancelled", source="stripe",
                     stripe_customer_id="cus_test123", stripe_subscription_id="sub_devgenie_old"),
    ])
    await db_session.commit()
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel", "product": "devgenie"})
    assert res.status_code == 422 and res.json()["error"]["code"] == "no_subscription"  # nothing live for devgenie yet

    await _activate(client, admin, "evt_p10")  # the live devgenie subscription: sub_evt_p10
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel", "product": "devgenie"})
    assert res.status_code == 200, res.text
    assert gateway.calls[-1] == ("create_portal_session", "cus_test123", "sub_evt_p10")


@pytest.mark.asyncio
async def test_a_cancelled_subscription_is_not_offered_the_cancel_flow(client, db_session, admin, gateway):
    sub = await _activate(client, admin, "evt_p11")
    assert (await _post(client, _updated("evt_p11_gone", sub, status="canceled", cancel_at_period_end=False))).status_code == 200
    res = await client.post(_portal_url(admin), headers=admin["headers"], json={"flow": "cancel", "product": "devgenie"})
    assert res.status_code == 422 and res.json()["error"]["code"] == "no_subscription"
