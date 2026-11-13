"""CR-020 Lite slice 1 (revised), BRIEF-173: any email registers, and a signed-in person can set and clear an
OPTIONAL company name and LinkedIn URL through one validated route (PUT /api/v1/me/signup-profile).

The owner withdrew the work-email rule on 2026-11-13: `x@gmail.com` registers like any address (S1, S2 are
regression witnesses that pass on main and go red only if someone adds a refusal). The two fields are personal
data: stored as given (trimmed), never enriched, never part of the server's entitlement answer (S9, S10).

Sections: S1 /register accepts webmail; S2 OAuth accepts webmail, and the write is durable; S3 set, trim, read back;
S4 clearing (PUT replaces); S5 identity is the bearer token only; S6 LinkedIn refusals; S7 company refusals;
S8 the closed 422 envelope and atomicity; S9 the internal lookup is byte-identical; S10 the Entra claims callout is
byte-identical; S11 the migration matches the model; S12 /help lists the route.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Membership, Organization, User
from app.models.enums import Role
from app.security.jwt import create_access_token

# Fixtures and helpers of the two existing suites (a fixture imported by name is visible in this module).
from tests.test_entra_claims import _call, _linked, entra_client, entra_settings  # noqa: F401
from tests.test_internal_entitlements import ENDPOINT as LOOKUP, SECRET, _linked_user, _params, svc  # noqa: F401
from tests.test_oauth_verified_email import _callback, userinfo  # noqa: F401

ROUTE = "/api/v1/me/signup-profile"
ME = "/api/v1/me"
GOOD_URL = "https://www.linkedin.com/in/x"
ROOT = Path(__file__).resolve().parents[1]


async def _user(session, email, slug):
    user = User(email=email, normalized_email=email, is_verified=True)
    org = Organization(name=f"{email} org", slug=slug)
    session.add_all([user, org])
    await session.flush()
    session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))
    await session.commit()
    return user, org


def _auth(user, org):
    token = create_access_token(get_settings(), str(user.id), user.email, "admin", str(org.id), [])[0]  # (token, ttl)
    return {"Authorization": f"Bearer {token}"}


async def _put(client, user, org, body, **headers):
    return await client.put(ROUTE, headers={**_auth(user, org), **headers}, json=body)


def _closed(res, code):
    assert res.status_code == 422, res.text
    body = res.json()
    assert set(body) == {"error"}, body  # never FastAPI's {"detail": [...]}
    assert set(body["error"]) == {"code", "message"}, body
    assert body["error"]["code"] == code, body
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


# --- S1, S2 any email registers (regression witnesses: green on main) ---------------------------------------


async def test_s1_register_accepts_a_webmail_address(client, db_session, sent_otps):
    assert get_settings().ALLOWED_EMAIL_DOMAINS == []
    res = await client.post("/api/v1/register", json={"email": "x@gmail.com", "password": "StrongPass1!"})
    assert res.status_code == 201, res.text
    row = (await db_session.execute(select(User).where(User.normalized_email == "x@gmail.com"))).scalar_one_or_none()
    assert row is not None


async def test_s2_oauth_callback_accepts_a_webmail_address(client, db_session, fake_redis, userinfo):
    userinfo.update({"sub": "g-webmail-1", "email": "x@gmail.com", "email_verified": True, "name": "X"})
    res = await _callback(client, fake_redis)
    assert res.status_code == 200, res.text
    assert res.json()["access_token"]
    row = (await db_session.execute(select(User).where(User.normalized_email == "x@gmail.com"))).scalar_one_or_none()
    assert row is not None


async def test_s2_email_code_completion_accepts_a_webmail_address(client, db_session, fake_redis, userinfo, sent_otps):
    userinfo.update({"sub": "g-webmail-2", "email": "x@gmail.com", "name": "X"})  # no email_verified flag: a code is emailed
    pending = await _callback(client, fake_redis, state="st-webmail")
    assert pending.status_code == 202, pending.text
    res = await client.post("/api/v1/oauth/verify", json={"pending_id": pending.json()["pending_id"], "otp": sent_otps[-1]})
    assert res.status_code == 200, res.text
    assert res.json()["access_token"]
    row = (await db_session.execute(select(User).where(User.normalized_email == "x@gmail.com"))).scalar_one_or_none()
    assert row is not None


async def test_s2_the_write_is_durable_in_production_session_model(fresh_client, session_factory):
    async with session_factory() as seed:
        user, org = await _user(seed, "durable@example.com", "durable-org")  # committed before the route's own session
    res = await _put(fresh_client, user, org, {"company_name": "  Acme Ltd ", "linkedin_url": f" {GOOD_URL} "})
    assert res.status_code == 200, res.text
    async with session_factory() as fresh:  # a NEW session: only committed rows count
        row = await fresh.get(User, user.id)
        assert row.company_name == "Acme Ltd"
        assert row.linkedin_url == GOOD_URL


# --- S3 set, trim, read back --------------------------------------------------------------------------------


async def test_s3_set_trim_and_read_back(client, db_session):
    user, org = await _user(db_session, "s3@example.com", "s3-org")
    res = await _put(client, user, org, {"company_name": "  Acme Ltd  ", "linkedin_url": GOOD_URL})
    assert res.status_code == 200, res.text
    assert res.json() == {"company_name": "Acme Ltd", "linkedin_url": GOOD_URL}
    got = await client.get(ME, headers=_auth(user, org))
    assert got.status_code == 200
    assert got.json()["company_name"] == "Acme Ltd" and got.json()["linkedin_url"] == GOOD_URL
    patched = await client.patch(ME, headers=_auth(user, org), json={})
    assert patched.json()["company_name"] == "Acme Ltd" and patched.json()["linkedin_url"] == GOOD_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://WWW.LinkedIn.com/company/acme-ltd/",
        "https://linkedin.com/in/a-b-9",
        "https://uk.linkedin.com/in/x",  # country subdomain (co-arch acceptance)
        "https://www.linkedin.com/in/a_b",  # underscore slug (co-arch acceptance)
        "https://www.linkedin.com/in/" + "a" * 227,  # exactly 255 characters
    ],
)
async def test_s3_valid_urls_are_accepted(client, db_session, url):
    user, org = await _user(db_session, "s3u@example.com", "s3u-org")
    res = await _put(client, user, org, {"linkedin_url": url})
    assert res.status_code == 200, res.text
    assert res.json()["linkedin_url"] == url


# --- S4 clearing: PUT replaces, an omitted or empty value clears ---------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"company_name": None, "linkedin_url": None},
        {"company_name": "", "linkedin_url": ""},
        {"company_name": "   ", "linkedin_url": "   "},
    ],
)
async def test_s4_empty_null_or_omitted_clears_both(client, db_session, body):
    user, org = await _user(db_session, "s4@example.com", "s4-org")
    assert (await _put(client, user, org, {"company_name": "Keep", "linkedin_url": GOOD_URL})).status_code == 200
    res = await _put(client, user, org, body)
    assert res.status_code == 200, res.text
    assert res.json() == {"company_name": None, "linkedin_url": None}
    await db_session.refresh(user)
    assert user.company_name is None and user.linkedin_url is None


async def test_s4_both_empty_is_accepted_on_a_new_account(client, db_session):
    user, org = await _user(db_session, "s4n@example.com", "s4n-org")
    assert user.company_name is None and user.linkedin_url is None  # optional: absent by default
    res = await _put(client, user, org, {})
    assert res.status_code == 200 and res.json() == {"company_name": None, "linkedin_url": None}


async def test_s4_setting_only_the_company_clears_the_other_field(client, db_session):
    user, org = await _user(db_session, "s4o@example.com", "s4o-org")
    await _put(client, user, org, {"company_name": "Acme", "linkedin_url": GOOD_URL})
    res = await _put(client, user, org, {"company_name": "Acme"})
    assert res.json() == {"company_name": "Acme", "linkedin_url": None}


# --- S5 identity is the bearer token only --------------------------------------------------------------------


async def test_s5_body_and_header_identity_are_ignored(client, db_session):
    a, a_org = await _user(db_session, "a@example.com", "a-org")
    b, b_org = await _user(db_session, "b@example.com", "b-org")
    body = {"user_id": str(b.id), "id": str(b.id), "email": b.email, "org_id": str(b_org.id), "company_name": "Mine"}
    res = await _put(client, a, a_org, body, **{"X-Org-Id": str(b_org.id)})
    assert res.status_code == 200, res.text
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.company_name == "Mine"
    assert b.company_name is None and b.linkedin_url is None


async def test_s5_no_or_bad_bearer_is_refused_and_writes_nothing(client, db_session):
    user, _ = await _user(db_session, "s5@example.com", "s5-org")
    none = await client.put(ROUTE, json={"company_name": "X"})
    bad = await client.put(ROUTE, headers={"Authorization": "Bearer not-a-token"}, json={"company_name": "X"})
    assert none.status_code == 401 and bad.status_code == 401
    await db_session.refresh(user)
    assert user.company_name is None


# --- S6 LinkedIn refusals -------------------------------------------------------------------------------------

BAD_URLS = [
    "http://www.linkedin.com/in/x",
    "http://evil.example/in/x",  # the brief's named case
    "https://evil.example/in/x",
    "https://linkedin.com.evil.example/in/x",  # the brief's named case
    "https://notlinkedin.com/in/x",
    "https://www.notlinkedin.com/in/x",
    "https://linkedin.com@evil.example/in/x",
    "https://evil@www.linkedin.com/in/x",
    "https://linkedin.com:8443/in/x",
    "https://ukk.linkedin.com/in/x",
    "https://u.linkedin.com/in/x",
    "https://evil.uk.linkedin.com/in/x",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com/in/../x",
    "https://www.linkedin.com/in/a/b",
    "https://www.linkedin.com/jobs/x",
    "https://www.linkedin.com/in/x?trk=1",
    "https://www.linkedin.com/in/x#a",
    "https://www.linkedin.com/in/%2e%2e",
    "https://www.linkedin.com/in/a b",
    "https://www.linkedin.com" + chr(92) + "in" + chr(92) + "x",  # backslashes
    "https://www.linkedin.com./in/x",
    "https://www.linkedinXcom/in/x",
    "https://www.l" + chr(0x131) + "nkedin.com/in/x",  # dotless i
    "https://www.lin" + chr(0x212A) + "edin.com/in/x",  # Kelvin sign
    "linkedin.com/in/x",
    "https://www.linkedin.com/in/x\n/in/y",
    12345,
    ["x"],
    {"a": 1},
    True,
]


@pytest.mark.parametrize("url", BAD_URLS, ids=[repr(u)[:60] for u in BAD_URLS])
async def test_s6_bad_linkedin_urls_are_refused_and_nothing_changes(client, db_session, url):
    user, org = await _user(db_session, "s6@example.com", "s6-org")
    assert (await _put(client, user, org, {"company_name": "Keep Co", "linkedin_url": GOOD_URL})).status_code == 200
    res = await _put(client, user, org, {"company_name": "New Co", "linkedin_url": url})
    _closed(res, "linkedin_url_invalid")
    await db_session.refresh(user)
    assert user.company_name == "Keep Co" and user.linkedin_url == GOOD_URL


async def test_s6_the_length_cap_applies_after_trimming(client, db_session):
    user, org = await _user(db_session, "s6t@example.com", "s6t-org")
    padded = "   https://www.linkedin.com/in/" + "a" * 227 + "   "  # 255 once trimmed, 261 as sent
    res = await _put(client, user, org, {"linkedin_url": padded})
    assert res.status_code == 200, res.text
    assert len(res.json()["linkedin_url"]) == 255


async def test_s6_a_256_character_url_is_too_long(client, db_session):
    user, org = await _user(db_session, "s6l@example.com", "s6l-org")
    res = await _put(client, user, org, {"linkedin_url": "https://www.linkedin.com/in/" + "a" * 228})
    _closed(res, "linkedin_url_too_long")


# --- S7 company refusals --------------------------------------------------------------------------------------


async def test_s7_company_length_is_capped_at_120(client, db_session):
    user, org = await _user(db_session, "s7@example.com", "s7-org")
    assert (await _put(client, user, org, {"company_name": "c" * 120})).status_code == 200
    _closed(await _put(client, user, org, {"company_name": "c" * 121}), "company_name_too_long")


async def test_s7_the_company_cap_applies_after_trimming(client, db_session):
    user, org = await _user(db_session, "s7t@example.com", "s7t-org")
    res = await _put(client, user, org, {"company_name": "  " + "c" * 120 + "  "})  # 120 once trimmed, 124 as sent
    assert res.status_code == 200, res.text
    assert res.json()["company_name"] == "c" * 120


@pytest.mark.parametrize("value", ["Acme\nLtd", "Acme\tLtd", "Acme" + chr(0) + "Ltd", 123, {"a": 1}, ["x"], True])
async def test_s7_control_characters_and_non_strings_are_invalid(client, db_session, value):
    user, org = await _user(db_session, "s7c@example.com", "s7c-org")
    _closed(await _put(client, user, org, {"company_name": value}), "company_name_invalid")


# --- S8 the closed envelope for a bad body, and atomicity ----------------------------------------------------


@pytest.mark.parametrize("raw", ["not json", "[1]", '"x"', "123", "null", ""])
async def test_s8_a_body_that_is_not_a_json_object_is_profile_body_invalid(client, db_session, raw):
    user, org = await _user(db_session, "s8@example.com", "s8-org")
    res = await client.put(ROUTE, headers={**_auth(user, org), "Content-Type": "application/json"}, content=raw)
    _closed(res, "profile_body_invalid")


async def test_s8_a_refused_request_writes_nothing_not_even_the_valid_half(client, db_session):
    user, org = await _user(db_session, "s8a@example.com", "s8a-org")
    res = await _put(client, user, org, {"company_name": "Valid Co", "linkedin_url": "https://evil.example/in/x"})
    _closed(res, "linkedin_url_invalid")
    assert user.company_name is None  # the SAME session's identity map: an uncommitted partial assignment shows here
    assert user.linkedin_url is None
    await db_session.refresh(user)
    assert user.company_name is None


# --- S9, S10 the server's answers are byte-identical ----------------------------------------------------------


async def test_s9_the_internal_lookup_answer_is_byte_identical(svc, db_session):
    user, primary, _ = await _linked_user(db_session)
    headers = {"X-Service-Secret": SECRET}
    before = await svc.get(LOOKUP, params=_params(), headers=headers)
    assert before.status_code == 200
    res = await _put(svc, user, primary, {"company_name": "Acme Ltd", "linkedin_url": GOOD_URL})
    assert res.status_code == 200, res.text
    after = await svc.get(LOOKUP, params=_params(), headers=headers)
    assert after.content == before.content


async def test_s10_the_entra_claims_answer_is_byte_identical(entra_client, db_session, rsa_keypair):
    user, org = await _linked(db_session, "entra-sub-173", "p@customer.example")
    before = await _call(entra_client, rsa_keypair, "entra-sub-173", "p@customer.example")
    assert before.status_code == 200, before.text
    res = await _put(entra_client, user, org, {"company_name": "Acme Ltd", "linkedin_url": GOOD_URL})
    assert res.status_code == 200, res.text
    after = await _call(entra_client, rsa_keypair, "entra-sub-173", "p@customer.example")
    assert after.content == before.content


# --- S11 the migration matches the model ---------------------------------------------------------------------

REVISION = "20261113_000006"
PREVIOUS = "20260914_000005"


def _migration():
    path = ROOT / "alembic" / "versions" / "20261113_000006_user_company_linkedin.py"
    spec = importlib.util.spec_from_file_location("mig_user_company_linkedin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_s11_one_head_and_it_follows_the_stripe_revision():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_s11_upgrade_adds_exactly_the_two_columns_and_downgrade_removes_them():
    mig = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("users")}
        assert set(cols) == {"id", "company_name", "linkedin_url"}
        for name in ("company_name", "linkedin_url"):
            assert cols[name]["nullable"] is True
            assert cols[name]["type"].length == User.__table__.c[name].type.length
        assert (cols["company_name"]["type"].length, cols["linkedin_url"]["type"].length) == (120, 255)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert {c["name"] for c in sa.inspect(conn).get_columns("users")} == {"id"}


# --- S12 /help lists the route -------------------------------------------------------------------------------


async def test_s12_help_lists_the_route(client):
    res = await client.get("/api/v1/help")
    assert res.status_code == 200
    listed = {(e["method"], e["path"]) for e in res.json()["endpoints"]["authenticated"]}
    assert ("PUT", f"{get_settings().API_V1_PREFIX}/me/signup-profile") in listed
