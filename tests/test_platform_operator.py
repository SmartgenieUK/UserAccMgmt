"""BUG-06 (CR-208, BRIEF-014): no sign-up can act as a platform admin.

Every sign-up is ADMIN of its own new organisation and of nothing else. The cross-organisation routes
(list users, disable a user, set an organisation's plan) need `platform:admin`, which only a user named in
PLATFORM_OPERATOR_USER_IDS gets. An email domain never grants membership or authority.
"""
from __future__ import annotations

import json
import time
import uuid

import jwt
import pytest
from sqlalchemy import select

from app.api.v1.health import api_help
from app.core.config import get_settings
from app.main import app
from app.models import Application, Membership, Role
from app.security.hashing import hash_token
from app.security.jwt import decode_access_token
from app.security.permissions import ROLE_SCOPES
from app.services import oauth_providers
from app.services.stripe_gateway import StripeGateway, get_stripe_gateway

PASSWORD = "StrongPass1!"
PRODUCT = "devgenie"
TENANT_ID = "tenant-bug06"
ISSUER = f"https://{TENANT_ID}.ciamlogin.com/{TENANT_ID}/v2.0"
CLIENT_ID = "client-bug06"


class FakeStripeGateway(StripeGateway):
    def create_customer(self, name: str, email: str, metadata: dict) -> str:
        return "cus_bug06"

    def create_checkout_session(self, customer_id, price_id, success_url, cancel_url, metadata) -> str:
        return "https://checkout.stripe.test/session"

    def create_portal_session(self, customer_id, return_url) -> str:
        return "https://portal.stripe.test/session"


@pytest.fixture()
def env(client, fake_redis, rsa_keypair, monkeypatch):
    """One settings object for Stripe, the Entra provider and the operator list; `env.operators(...)` sets the list."""
    private_pem, public_pem = rsa_keypair
    state = {
        "settings": get_settings().model_copy(
            update={
                "STRIPE_PRICE_MAP": {"price_pro": "devgenie:pro"},
                "STRIPE_CHECKOUT_SUCCESS_URL": "http://test/success",
                "STRIPE_CHECKOUT_CANCEL_URL": "http://test/cancel",
                "STRIPE_PORTAL_RETURN_URL": "http://test/portal",
                "ENTRA_TENANT_DOMAIN": "testtenant",
                "ENTRA_TENANT_ID": TENANT_ID,
                "ENTRA_CLIENT_ID": CLIENT_ID,
                "ENTRA_CLIENT_SECRET": "x",
                "ENTRA_ISSUER": ISSUER,
                "ENTRA_SIGNING_PUBLIC_KEY_PEM": public_pem.decode(),
                "PLATFORM_OPERATOR_USER_IDS": [],
            }
        ),
        "id_token": None,
        "userinfo": {},
    }
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    app.dependency_overrides[get_stripe_gateway] = lambda: FakeStripeGateway(state["settings"])

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return dict(state["userinfo"])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def fetch_token(self, *args, **kwargs):
            return {"access_token": "at", "id_token": state["id_token"]}

        async def get(self, url):
            return FakeResp()

        async def aclose(self):
            return None

    monkeypatch.setattr(oauth_providers, "AsyncOAuth2Client", FakeClient)

    class Env:
        def operators(self, *user_ids: str) -> None:
            state["settings"] = state["settings"].model_copy(update={"PLATFORM_OPERATOR_USER_IDS": list(user_ids)})

        async def oauth_signup(self, oid: str, email: str) -> dict:
            now = int(time.time())
            state["id_token"] = jwt.encode(
                {"iss": ISSUER, "aud": CLIENT_ID, "sub": f"pw-{oid}", "oid": oid, "iat": now, "exp": now + 300},
                private_pem,
                algorithm="RS256",
            )
            state["userinfo"] = {"sub": f"pw-{oid}", "email": email, "email_verified": True, "name": email}
            fake_redis.store[f"oauth:state:st-{oid}"] = json.dumps({"code_verifier": "v", "redirect_uri": "http://cb"})
            r = await client.post("/api/v1/oauth/entra_external/callback", json={"code": "c", "state": f"st-{oid}"})
            assert r.status_code == 200, r.text
            return await _whoami(client, r.json()["access_token"])

    yield Env()
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_stripe_gateway, None)


async def _whoami(client, access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    me = await client.get("/api/v1/me", headers=headers)
    orgs = await client.get("/api/v1/orgs", headers=headers)
    assert me.status_code == 200 and orgs.status_code == 200, (me.text, orgs.text)
    return {"headers": headers, "user_id": me.json()["id"], "org_id": orgs.json()[0]["id"], "orgs": orgs.json()}


async def _password_signup(client, sent_otps, email: str) -> dict:
    before = len(sent_otps)
    assert (await client.post("/api/v1/register", json={"email": email, "password": PASSWORD})).status_code == 201
    assert (await client.post("/api/v1/verify-email", json={"email": email, "otp": sent_otps[before]})).status_code == 200
    return await _password_login(client, email)


async def _password_login(client, email: str) -> dict:
    login = await client.post("/api/v1/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return await _whoami(client, login.json()["access_token"])


async def _signup(kind: str, client, sent_otps, env, email: str, oid: str) -> dict:
    if kind == "password":
        return await _password_signup(client, sent_otps, email)
    return await env.oauth_signup(oid, email)


async def _tier(client, user: dict) -> str:
    r = await client.get("/api/v1/entitlements/me", params={"product": PRODUCT}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["tier"]


async def _platform_routes(client, caller: dict, target: dict) -> list[int]:
    """Status of each cross-organisation route when `caller` aims it at `target`."""
    listing = await client.get("/api/v1/admin/users", headers=caller["headers"])
    disable = await client.patch(
        f"/api/v1/admin/users/{target['user_id']}/disable", json={"disable": True}, headers=caller["headers"]
    )
    plan = await client.put(
        f"/api/v1/orgs/{target['org_id']}/entitlements/{PRODUCT}", json={"tier": "enterprise"}, headers=caller["headers"]
    )
    return [listing.status_code, disable.status_code, plan.status_code]


@pytest.mark.parametrize("kind", ["password", "oauth"])
async def test_a_sign_up_is_refused_on_every_platform_route(client, sent_otps, env, kind):
    user = await _signup(kind, client, sent_otps, env, "alice@org1.example", "oid-alice")
    assert await _platform_routes(client, user, user) == [403, 403, 403]
    assert await _tier(client, user) == "lite"  # their own plan is unchanged
    me = await client.get("/api/v1/me", headers=user["headers"])
    assert me.status_code == 200  # and they did not disable themselves


@pytest.mark.parametrize("kind", ["password", "oauth"])
async def test_a_sign_up_keeps_its_own_org_billing_and_seats(client, sent_otps, env, kind):
    user = await _signup(kind, client, sent_otps, env, "bob@org1.example", "oid-bob")
    other = await _password_signup(client, sent_otps, "carol@org2.example")
    own = f"/api/v1/orgs/{user['org_id']}"

    checkout = await client.post(f"{own}/billing/checkout-session", json={"price_id": "price_pro"}, headers=user["headers"])
    assert checkout.status_code == 200, checkout.text
    portal = await client.post(f"{own}/billing/portal-session", headers=user["headers"])
    assert portal.status_code == 200, portal.text

    # Seats need a seated plan; the operator sets it (the sign-up cannot).
    operator = await _password_signup(client, sent_otps, "ops@platform.example")
    env.operators(operator["user_id"])
    operator = await _password_login(client, "ops@platform.example")
    assert (
        await client.put(f"{own}/entitlements/{PRODUCT}", json={"tier": "pro", "seats": 2}, headers=operator["headers"])
    ).status_code == 200
    seats = f"{own}/entitlements/{PRODUCT}/seats"
    assert (await client.post(seats, json={"user_id": user["user_id"]}, headers=user["headers"])).status_code == 200
    assert (await client.delete(f"{seats}/{user['user_id']}", headers=user["headers"])).status_code == 200

    # ...and none of it on someone else's organisation.
    theirs = f"/api/v1/orgs/{other['org_id']}"
    assert (
        await client.post(f"{theirs}/billing/checkout-session", json={"price_id": "price_pro"}, headers=user["headers"])
    ).status_code == 403
    assert (await client.post(f"{theirs}/billing/portal-session", headers=user["headers"])).status_code == 403
    assert (
        await client.post(f"{theirs}/entitlements/{PRODUCT}/seats", json={"user_id": user["user_id"]}, headers=user["headers"])
    ).status_code == 403


@pytest.mark.parametrize("kind", ["password", "oauth"])
async def test_same_email_domain_grants_nothing(client, db_session, sent_otps, env, kind):
    first = await _signup(kind, client, sent_otps, env, "dan@acme.example", "oid-dan")
    second = await _signup(kind, client, sent_otps, env, "erin@acme.example", "oid-erin")

    assert first["org_id"] != second["org_id"]
    for user in (first, second):
        assert [o["id"] for o in user["orgs"]] == [user["org_id"]]  # a member of their own organisation only
        roles = (await db_session.execute(select(Membership).where(Membership.user_id == user["user_id"]))).scalars().all()
        assert [(str(m.org_id), m.role) for m in roles] == [(user["org_id"], Role.ADMIN)]

    for caller, target in ((first, second), (second, first)):
        assert await _platform_routes(client, caller, target) == [403, 403, 403]
        members = await client.get(f"/api/v1/orgs/{target['org_id']}/entitlements", headers=caller["headers"])
        assert members.status_code == 403
        assert await _tier(client, target) == "lite"
        assert (await client.get("/api/v1/me", headers=target["headers"])).status_code == 200


async def test_the_named_operator_succeeds_on_every_platform_route(client, sent_otps, env):
    customer = await _password_signup(client, sent_otps, "frank@cust.example")
    operator = await _password_signup(client, sent_otps, "ops@platform.example")
    env.operators(operator["user_id"])
    operator = await _password_login(client, "ops@platform.example")  # scopes are resolved when the token is minted

    assert await _platform_routes(client, operator, customer) == [200, 200, 200]
    assert await _tier(client, customer) == "enterprise"
    assert (await client.get("/api/v1/me", headers=customer["headers"])).status_code == 401  # disabled


async def test_an_unknown_org_is_not_found_for_the_operator(client, sent_otps, env):
    operator = await _password_signup(client, sent_otps, "ops@platform.example")
    env.operators(operator["user_id"])
    operator = await _password_login(client, "ops@platform.example")
    r = await client.put(
        f"/api/v1/orgs/00000000-0000-0000-0000-000000000000/entitlements/{PRODUCT}",
        json={"tier": "pro"},
        headers=operator["headers"],
    )
    assert r.status_code == 404


async def test_an_empty_operator_list_grants_nobody(client, sent_otps, env):
    customer = await _password_signup(client, sent_otps, "gina@cust.example")
    would_be = await _password_signup(client, sent_otps, "ops@platform.example")
    env.operators()
    assert await _platform_routes(client, would_be, customer) == [403, 403, 403]


async def test_removing_an_operator_takes_effect_on_their_existing_token(client, sent_otps, env):
    customer = await _password_signup(client, sent_otps, "hal@cust.example")
    operator = await _password_signup(client, sent_otps, "ops@platform.example")
    env.operators(operator["user_id"])
    operator = await _password_login(client, "ops@platform.example")
    env.operators()
    assert await _platform_routes(client, operator, customer) == [403, 403, 403]


async def test_an_app_cannot_be_created_or_updated_with_platform_admin(client, db_session, sent_otps, env):
    """An org admin cannot store `platform:admin` on an application, at create or at update (R7-F1)."""
    user = await _password_signup(client, sent_otps, "ivy@org1.example")
    apps = f"/api/v1/orgs/{user['org_id']}/apps"
    created = await client.post(
        apps, json={"name": "sneaky", "allowed_scopes": ["platform:admin", "billing:write"]}, headers=user["headers"]
    )
    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "reserved_scope"
    stored = (await db_session.execute(select(Application).where(Application.org_id == user["org_id"]))).scalars().all()
    assert stored == []

    plain = await client.post(apps, json={"name": "plain", "allowed_scopes": ["billing:write"]}, headers=user["headers"])
    assert plain.status_code == 201, plain.text
    patched = await client.patch(
        f"{apps}/{plain.json()['id']}", json={"allowed_scopes": ["platform:admin"]}, headers=user["headers"]
    )
    assert patched.status_code == 422, patched.text
    assert patched.json()["error"]["code"] == "reserved_scope"
    after = await client.get(f"{apps}/{plain.json()['id']}", headers=user["headers"])
    assert after.json()["allowed_scopes"] == ["billing:write"]


@pytest.mark.parametrize("requested", [["platform:admin", "billing:write"], None], ids=["explicit", "default"])
async def test_a_stored_platform_admin_app_mints_no_platform_admin_token(client, db_session, sent_otps, env, requested):
    """An application stored with `platform:admin` before the create-time refusal still never gets it in a token."""
    user = await _password_signup(client, sent_otps, "jo@org1.example")
    secret = "cgs_bug06-legacy"
    db_session.add(
        Application(
            org_id=uuid.UUID(user["org_id"]),
            name="legacy",
            client_id="cg_bug06-legacy",
            secret_hash=hash_token(secret),
            redirect_uris=[],
            allowed_scopes=["platform:admin", "billing:write"],
        )
    )
    await db_session.flush()
    body = {"client_id": "cg_bug06-legacy", "client_secret": secret}
    if requested is not None:
        body["scopes"] = requested
    token = await client.post("/api/v1/auth/token", json=body)
    assert token.status_code == 200, token.text
    assert token.json()["scopes"] == ["billing:write"]
    claims = decode_access_token(get_settings(), token.json()["access_token"])
    assert claims["token_type"] == "client"
    assert claims["scopes"] == ["billing:write"]
    app_caller = {"headers": {"Authorization": f"Bearer {token.json()['access_token']}"}}
    assert await _platform_routes(client, app_caller, user) == [403, 403, 403]


async def test_no_org_role_carries_platform_scopes():
    for role, scopes in ROLE_SCOPES.items():
        assert not {"platform:admin", "admin:users:read", "admin:users:write"} & set(scopes), role


async def test_help_scope_catalogue_matches_the_role_table():
    body = await api_help(settings=get_settings())
    assert body["roles"] == {role.value: scopes for role, scopes in ROLE_SCOPES.items()}
    granted = {s for scopes in ROLE_SCOPES.values() for s in scopes} | {"platform:admin"}
    assert set(body["scopes"]) == granted
    required = {s for group in body["endpoints"].values() for e in group for s in e.get("scopes", [])}
    assert required <= set(body["scopes"])
    admin_routes = {(e["method"], e["path"].rsplit("/v1", 1)[-1]): e["scopes"] for e in body["endpoints"]["admin"]}
    assert admin_routes[("GET", "/admin/users")] == ["platform:admin"]
    assert admin_routes[("PATCH", "/admin/users/{user_id}/disable")] == ["platform:admin"]
    assert admin_routes[("PUT", "/orgs/{org_id}/entitlements/{product}")] == ["platform:admin"]
