"""BRIEF-183 Welcome witnesses U1-U5; run python -m pytest tests/test_signup_profile.py -q.
Existing any-email, bearer, lookup/claims byte equality and help regressions retained.
Named production mutants are identified in test names; co-arch applies them separately.
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
from tests.test_oauth_verified_email import _callback, userinfo, welcome_offline  # noqa: F401

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



# BRIEF-183 U1-U5. No feature implementation, no live provider/Stripe/network calls.
import hashlib
import itertools
import string
from app.core.exceptions import ValidationError
from app.services.user_service import UserService
from app.services import user_service as profile_service

FIELDS = ("first_name", "last_name", "company_name", "role", "country", "phone", "linkedin_url", "github_url")
REQUIRED = ("first_name", "company_name", "role", "country")
CAPS = {"first_name": 80, "last_name": 80, "company_name": 120, "role": 120, "phone": 32, "linkedin_url": 255, "github_url": 255}


def good(**changes):
    return {"first_name": "Zoë", "last_name": "李", "company_name": "Acme Ltd", "role": "Engineer", "country": "GB",
            "phone": "+442079460123", "linkedin_url": GOOD_URL, "github_url": "https://github.com/zoe-lee", **changes}


def snapshot(user):
    return {f: getattr(user, f) for f in (*FIELDS, "profile_completed")}


@pytest.mark.parametrize("field", REQUIRED)
@pytest.mark.parametrize("value", ["OMIT", None, "", "   "])
async def test_u1_required_fields_allow_first_blank_allow_company_blank_allow_role_blank_allow_country_blank(client, db_session, field, value):
    user, org = await _user(db_session, "required@example.com", "required")
    body = good(**{field: value})
    if value == "OMIT":
        del body[field]
    _closed(await _put(client, user, org, body), f"{field}_required")
    await db_session.refresh(user)
    assert user.profile_completed is False
    assert all(getattr(user, f) is None for f in FIELDS)


@pytest.mark.parametrize("last", [None, "", "   ", "OMIT"])
async def test_u1_require_last_single_name_is_valid(client, db_session, last):
    user, org = await _user(db_session, "single@example.com", "single")
    body = good(last_name=last)
    if last == "OMIT":
        del body["last_name"]
    r = await _put(client, user, org, body)
    assert r.status_code == 200, r.text
    assert r.json() == {**good(last_name=None), "profile_completed": True}


async def test_u1_omit_commit_omit_read_field_eight_fields_and_completion_are_durable(fresh_client, session_factory):
    async with session_factory() as seed:
        user, org = await _user(seed, "durable@example.com", "durable")
    body = {k: f"  {v}  " for k, v in good().items()}
    body["country"] = " gb "
    r = await _put(fresh_client, user, org, body)
    assert r.status_code == 200, r.text
    expected = {**good(), "profile_completed": True}
    assert r.json() == expected
    async with session_factory() as fresh:
        assert snapshot(await fresh.get(User, user.id)) == expected
    for r in (await fresh_client.get(ME, headers=_auth(user, org)), await fresh_client.patch(ME, headers=_auth(user, org), json={})): 
        assert r.status_code == 200, r.text
        assert {k: r.json()[k] for k in expected} == expected


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [123, True, [], {}, "A\nB", "A\tB", "A\x00B", "A\x7fB"])
async def test_u1_types_and_controls_are_closed_field_errors(client, db_session, field, value):
    user, org = await _user(db_session, "invalid@example.com", "invalid")
    _closed(await _put(client, user, org, good(**{field: value})), f"{field}_invalid")


@pytest.mark.parametrize("field,cap", CAPS.items())
async def test_u2_drop_length_cap_per_field(client, db_session, field, cap):
    user, org = await _user(db_session, "cap@example.com", "cap")
    _closed(await _put(client, user, org, good(**{field: "x" * (cap + 1)})), f"{field}_too_long")


@pytest.mark.parametrize("field", ["first_name", "last_name", "company_name", "role"])
async def test_u1_text_cap_boundary_and_unicode(client, db_session, field):
    user, org = await _user(db_session, "unicode@example.com", "unicode")
    value = "李" * CAPS[field]
    r = await _put(client, user, org, good(**{field: f"  {value}  "}))
    assert r.status_code == 200, r.text
    assert r.json()[field] == value


@pytest.mark.parametrize("completed", [False, True])
async def test_u1_partial_assign_validation_precedes_every_assignment(client, db_session, completed):
    user, org = await _user(db_session, "atomic@example.com", "atomic")
    if completed:
        assert (await _put(client, user, org, good())).status_code == 200
    before = snapshot(user)
    body = good(first_name="Changed", last_name="Changed", company_name="Changed", role="Changed", country="NZ", github_url="https://github.com/name/repo")
    # Observe the SAME object before route exception/transaction cleanup could hide an assignment.
    service = UserService(db_session, get_settings(), None)
    with pytest.raises(ValidationError):
        await service.set_signup_profile(user, body)
    assert snapshot(user) == before
    _closed(await _put(client, user, org, body), "github_url_invalid")
    assert snapshot(user) == before
    await db_session.refresh(user)
    assert snapshot(user) == before


@pytest.mark.parametrize("raw", ["not json", "[1]", '"x"', "123", "null", ""])
async def test_u1_bad_body_has_closed_error(client, db_session, raw):
    user, org = await _user(db_session, "body@example.com", "body")
    _closed(await client.put(ROUTE, headers={**_auth(user, org), "Content-Type": "application/json"}, content=raw), "profile_body_invalid")


async def test_u1_validation_order_and_old_two_field_saves_refused(client, db_session):
    user, org = await _user(db_session, "order@example.com", "order")
    _closed(await _put(client, user, org, {"company_name": "Old portal", "linkedin_url": GOOD_URL}), "first_name_required")
    body = {f: [] for f in FIELDS}
    for f in FIELDS:
        _closed(await _put(client, user, org, body), f"{f}_invalid")
        body[f] = good()[f]


async def test_u1_optional_omission_clears_but_required_values_remain(client, db_session):
    user, org = await _user(db_session, "replace@example.com", "replace")
    assert (await _put(client, user, org, good())).status_code == 200
    body = {k: v for k, v in good().items() if k in REQUIRED}
    r = await _put(client, user, org, body)
    assert r.status_code == 200, r.text
    assert r.json() == {**body, **{k: None for k in FIELDS if k not in REQUIRED}, "profile_completed": True}


# Literal reference from b183-country-reference.md, NOT imported from a production table.
COUNTRY_REFERENCE = """AD:376 AE:971 AF:93 AG:1 AI:1 AL:355 AM:374 AO:244 AQ:- AR:54 AS:1 AT:43 AU:61 AW:297 AX:358 AZ:994
BA:387 BB:1 BD:880 BE:32 BF:226 BG:359 BH:973 BI:257 BJ:229 BL:590 BM:1 BN:673 BO:591 BQ:599 BR:55 BS:1 BT:975 BV:- BW:267 BY:375 BZ:501
CA:1 CC:61 CD:243 CF:236 CG:242 CH:41 CI:225 CK:682 CL:56 CM:237 CN:86 CO:57 CR:506 CU:53 CV:238 CW:599 CX:61 CY:357 CZ:420
DE:49 DJ:253 DK:45 DM:1 DO:1 DZ:213
EC:593 EE:372 EG:20 EH:212 ER:291 ES:34 ET:251
FI:358 FJ:679 FK:500 FM:691 FO:298 FR:33
GA:241 GB:44 GD:1 GE:995 GF:594 GG:44 GH:233 GI:350 GL:299 GM:220 GN:224 GP:590 GQ:240 GR:30 GS:500 GT:502 GU:1 GW:245 GY:592
HK:852 HM:- HN:504 HR:385 HT:509 HU:36
ID:62 IE:353 IL:972 IM:44 IN:91 IO:246 IQ:964 IR:98 IS:354 IT:39
JE:44 JM:1 JO:962 JP:81
KE:254 KG:996 KH:855 KI:686 KM:269 KN:1 KP:850 KR:82 KW:965 KY:1 KZ:7
LA:856 LB:961 LC:1 LI:423 LK:94 LR:231 LS:266 LT:370 LU:352 LV:371 LY:218
MA:212 MC:377 MD:373 ME:382 MF:590 MG:261 MH:692 MK:389 ML:223 MM:95 MN:976 MO:853 MP:1 MQ:596 MR:222 MS:1 MT:356 MU:230 MV:960 MW:265 MX:52 MY:60 MZ:258
NA:264 NC:687 NE:227 NF:672 NG:234 NI:505 NL:31 NO:47 NP:977 NR:674 NU:683 NZ:64
OM:968
PA:507 PE:51 PF:689 PG:675 PH:63 PK:92 PL:48 PM:508 PN:64 PR:1 PS:970 PT:351 PW:680 PY:595
QA:974
RE:262 RO:40 RS:381 RU:7 RW:250
SA:966 SB:677 SC:248 SD:249 SE:46 SG:65 SH:- SI:386 SJ:47 SK:421 SL:232 SM:378 SN:221 SO:252 SR:597 SS:211 ST:239 SV:503 SX:1 SY:963 SZ:268
TC:1 TD:235 TF:- TG:228 TH:66 TJ:992 TK:690 TL:670 TM:993 TN:216 TO:676 TR:90 TT:1 TV:688 TW:886 TZ:255
UA:380 UG:256 UM:- US:1 UY:598 UZ:998
VA:39 VC:1 VE:58 VG:1 VI:1 VN:84 VU:678
WF:681 WS:685
YE:967 YT:262
ZA:27 ZM:260 ZW:263
"""
COUNTRY_SHA256 = "bf85ae25bac0a1cbaeeff8134e7919818ecfc4e56f98506210039832f1ee7703"
COUNTRIES = {k: None if v == "-" else "+" + v for k, v in (entry.split(":") for entry in COUNTRY_REFERENCE.split())}


async def test_u2_remove_same_country_both_tables_wrong_country_prefix(client, db_session):
    assert hashlib.sha256(COUNTRY_REFERENCE.encode()).hexdigest() == COUNTRY_SHA256
    assert len(COUNTRIES) == 249
    # Public module constant is the table seam for the planned service implementation.
    # Equality is against this literal oracle, never the portal table or a generated production fixture.
    assert getattr(profile_service, "COUNTRY_CALLING_CODES", None) == COUNTRIES
    user, org = await _user(db_session, "world@example.com", "world")
    for country, prefix in COUNTRIES.items():
        for phone in ("", prefix) if prefix else ("",):
            r = await _put(client, user, org, good(country=country, phone=phone))
            assert r.status_code == 200, (country, prefix, r.text)
            assert r.json()["country"] == country
            assert r.json()["phone"] is None, (country, prefix, r.json())


async def test_u2_accept_unknown_country_closed_complete_set(client, db_session):
    user, org = await _user(db_session, "unknown@example.com", "unknown")
    unknown = {"".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)} - COUNTRIES.keys()
    for country in sorted(unknown) + ["GBR", "1A", "UK", "XK"]:
        _closed(await _put(client, user, org, good(country=country)), "country_invalid")


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None), ("+44", None), (" +44 ", None),
    ("+44 (20) 7946-0123", "+442079460123"), ("+1.202.555.0123", "+12025550123"),
    ("+44 (0)20 7946 0123", "+4402079460123"),  # no guessed national trunk removal
    ("+1234567", "+1234567"), ("+123456789012345", "+123456789012345"),
    ("+1" + "-" * 24 + "234567", "+1234567"),  # 32 raw characters, 7 digits
])
async def test_u2_phone_normalises_and_preserves_explicit_foreign_prefix(client, db_session, raw, expected):
    user, org = await _user(db_session, "phone@example.com", "phone")
    r = await _put(client, user, org, good(phone=raw))
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == expected


@pytest.mark.parametrize("phone", ["call me", "02079460123", "+01234567", "+123456", "+1234567890123456", "+44 ext 123", "++442079460123", "+１２３４５６７", "+44/2079460123"])
async def test_u2_accept_phone_text(client, db_session, phone):
    user, org = await _user(db_session, "badphone@example.com", "badphone")
    _closed(await _put(client, user, org, good(phone=phone)), "phone_invalid")


@pytest.mark.parametrize("url", ["https://WWW.LinkedIn.com/in/acme-ltd/", "https://linkedin.com/in/a-b-9", "https://uk.linkedin.com/in/x", "https://www.linkedin.com/in/a_b", "https://www.linkedin.com/in/" + "a" * 227])
async def test_u2_linkedin_profile_boundaries(client, db_session, url):
    user, org = await _user(db_session, "linkedin@example.com", "linkedin")
    r = await _put(client, user, org, good(linkedin_url="  " + url + "  "))
    assert r.status_code == 200, r.text
    assert r.json()["linkedin_url"] == url


@pytest.mark.parametrize("tail", ["?trk=1", "#a", "?utm_source=share&a=b#c"])
async def test_u2_copied_linkedin_tracking_is_removed(client, db_session, tail):
    user, org = await _user(db_session, "tracking@example.com", "tracking")
    r = await _put(client, user, org, good(linkedin_url=GOOD_URL + tail))
    assert r.status_code == 200, r.text
    assert r.json()["linkedin_url"] == GOOD_URL


BAD_LINKEDIN = [
    "http://www.linkedin.com/in/x",
    "http://evil.example/in/x",  # the brief's named case
    "https://evil.example/in/x",
    "https://linkedin.com.evil.example/in/x",  # the brief's named case
    "https://notlinkedin.com/in/x",
    "https://www.notlinkedin.com/in/x",
    "https://linkedin.com@evil.example/in/x",
    "https://evil@www.linkedin.com/in/x",
    "https://linkedin.com:8443/in/x",
    "https://evil.example?https://www.linkedin.com/in/x",
    "https://evil.example#https://www.linkedin.com/in/x",
    "https://ukk.linkedin.com/in/x",
    "https://u.linkedin.com/in/x",
    "https://evil.uk.linkedin.com/in/x",
    "https://www.linkedin.com/in/",
    "https://www.linkedin.com/in/../x",
    "https://www.linkedin.com/in/a/b",
    "https://www.linkedin.com/jobs/x",
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


@pytest.mark.parametrize("url", BAD_LINKEDIN + ["https://www.linkedin.com/company/acme", "https://www.linkedin.com/in/é", "https://www.linkedin.com/in/a%62", "https://www.linkedin.com:443/in/x"])
async def test_u2_accept_linkedin_company_accept_linkedin_lookalike(client, db_session, url):
    user, org = await _user(db_session, "badlinkedin@example.com", "badlinkedin")
    _closed(await _put(client, user, org, good(linkedin_url=url)), "linkedin_url_invalid")


@pytest.mark.parametrize("raw,expected", [("github.com/x", "https://github.com/x"), (" https://www.github.com/a-b/ ", "https://github.com/a-b"), ("github.com/" + "a" * 39, "https://github.com/" + "a" * 39)])
async def test_u2_github_canonical_form(client, db_session, raw, expected):
    user, org = await _user(db_session, "github@example.com", "github")
    r = await _put(client, user, org, good(github_url=raw))
    assert r.status_code == 200, r.text
    assert r.json()["github_url"] == expected


@pytest.mark.parametrize("url", ["http://github.com/x", "https://github.com/name/repo", "https://github.com.evil.test/name", "https://notgithub.com/name", "https://github.com@evil.test/name", "https://x@github.com/name", "https://github.com:443/name", "https://github.com/a%62", "https://github.com/é", "https://github.com/-name", "https://github.com/name-", "https://github.com/na--me", "https://github.com/" + "a" * 40, "https://github.com/", "https://github.com/../name"])
async def test_u2_accept_github_repo_and_host_username_grammar(client, db_session, url):
    user, org = await _user(db_session, "badgithub@example.com", "badgithub")
    _closed(await _put(client, user, org, good(github_url=url)), "github_url_invalid")


REVISION = "20261113_000008"
PREVIOUS = "20261113_000007"
ADDED = {"first_name": 80, "last_name": 80, "role": 120, "country": 2, "phone": 32, "github_url": 255}


def test_u3_wrong_down_revision_single_head():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_u3_omit_completion_column_wrong_legacy_backfill_and_reversible_upgrade():
    path = ROOT / "alembic/versions/20261113_000008_welcome_step_fields.py"
    assert path.is_file(), "U3: Welcome migration is missing"
    spec = importlib.util.spec_from_file_location("welcome_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.revision == REVISION and mig.down_revision == PREVIOUS
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, company_name VARCHAR(120), linkedin_url VARCHAR(255))"))
            conn.execute(sa.text("CREATE TABLE organizations (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE memberships (user_id INTEGER, org_id INTEGER)"))
            conn.execute(sa.text("CREATE TABLE plans (id INTEGER PRIMARY KEY, product VARCHAR(64), tier VARCHAR(20))"))
            conn.execute(sa.text("CREATE TABLE subscriptions (org_id INTEGER, plan_id INTEGER, product VARCHAR(64), status VARCHAR(32))"))
            # Populated/empty legacy users in BOTH Lite and paid cohorts.
            rows = [{"id": i, "co": "Legacy Co" if i % 2 else None, "li": GOOD_URL if i % 2 else None} for i in range(1, 5)]
            conn.execute(sa.text("INSERT INTO users VALUES (:id, :co, :li)"), rows)
            conn.execute(sa.text("INSERT INTO organizations VALUES (1), (2), (3), (4)"))
            conn.execute(sa.text("INSERT INTO memberships VALUES (1, 1), (2, 2), (3, 3), (4, 4)"))
            conn.execute(sa.text("INSERT INTO plans VALUES (1, 'devgenie', 'pro')"))
            conn.execute(sa.text("INSERT INTO subscriptions VALUES (3, 1, 'devgenie', 'active'), (4, 1, 'devgenie', 'active')"))
            before = conn.execute(sa.text("SELECT * FROM users ORDER BY id")).all()
            paid = conn.execute(sa.text("SELECT * FROM subscriptions ORDER BY org_id")).all()
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns("users")}
            assert set(cols) == {"id", "company_name", "linkedin_url", "profile_completed", *ADDED}
            for field, cap in ADDED.items():
                assert cols[field]["nullable"] is True
                assert cols[field]["type"].length == cap == User.__table__.c[field].type.length
            assert cols["profile_completed"]["nullable"] is False
            assert User.__table__.c.profile_completed.nullable is False
            assert conn.execute(sa.text("SELECT id, company_name, linkedin_url FROM users ORDER BY id")).all() == before
            assert conn.execute(sa.text("SELECT profile_completed FROM users ORDER BY id")).scalars().all() == [False] * 4
            conn.execute(sa.text("INSERT INTO users (id) VALUES (5)"))
            assert conn.execute(sa.text("SELECT profile_completed FROM users WHERE id=5")).scalar_one() == False
            assert conn.execute(sa.text("SELECT * FROM subscriptions ORDER BY org_id")).all() == paid
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
            assert {c["name"] for c in sa.inspect(conn).get_columns("users")} == {"id", "company_name", "linkedin_url"}
            assert conn.execute(sa.text("SELECT * FROM users WHERE id<5 ORDER BY id")).all() == before
            assert conn.execute(sa.text("SELECT * FROM subscriptions ORDER BY org_id")).all() == paid
    finally:
        engine.dispose()


async def test_u3_new_account_defaults_incomplete_and_only_valid_completion_sets_true(client, db_session):
    user, org = await _user(db_session, "default@example.com", "default")
    assert user.profile_completed is False
    _closed(await _put(client, user, org, {}), "first_name_required")
    await db_session.refresh(user)
    assert user.profile_completed is False
    assert (await _put(client, user, org, good())).status_code == 200
    await db_session.refresh(user)
    assert user.profile_completed is True


async def test_u5_use_body_identity_trust_body_completion(client, db_session):
    a, ao = await _user(db_session, "a@example.com", "a-org")
    b, bo = await _user(db_session, "b@example.com", "b-org")
    forged = {"user_id": str(b.id), "id": str(b.id), "email": b.email, "org_id": str(bo.id), "profile_completed": True}
    _closed(await _put(client, a, ao, forged), "first_name_required")
    await db_session.refresh(a)
    assert a.profile_completed is False
    r = await _put(client, a, ao, {**good(), **forged, "profile_completed": False}, **{"X-Org-Id": str(bo.id)})
    assert r.status_code == 200, r.text
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert snapshot(a) == {**good(), "profile_completed": True}
    assert snapshot(b) == {**dict.fromkeys(FIELDS), "profile_completed": False}
    assert a.email == "a@example.com" and b.email == "b@example.com"


async def test_u5_generic_patch_cannot_bypass_welcome(client, db_session):
    user, org = await _user(db_session, "patch@example.com", "patch")
    before = snapshot(user)
    r = await client.patch(ME, headers=_auth(user, org), json={**good(), "profile_completed": True})
    assert r.status_code in (200, 422)
    await db_session.refresh(user)
    assert snapshot(user) == before


async def test_u5_no_or_bad_bearer_writes_nothing(client, db_session):
    user, _ = await _user(db_session, "bearer@example.com", "bearer")
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        assert (await client.put(ROUTE, headers=headers, json=good())).status_code == 401
    await db_session.refresh(user)
    assert snapshot(user) == {**dict.fromkeys(FIELDS), "profile_completed": False}

async def test_s9_the_internal_lookup_answer_is_byte_identical(svc, db_session):
    user, primary, _ = await _linked_user(db_session)
    headers = {"X-Service-Secret": SECRET}
    before = await svc.get(LOOKUP, params=_params(), headers=headers)
    assert before.status_code == 200
    res = await _put(svc, user, primary, good())
    assert res.status_code == 200, res.text
    after = await svc.get(LOOKUP, params=_params(), headers=headers)
    assert after.content == before.content


async def test_s10_the_entra_claims_answer_is_byte_identical(entra_client, db_session, rsa_keypair):
    user, org = await _linked(db_session, "entra-sub-173", "p@customer.example")
    before = await _call(entra_client, rsa_keypair, "entra-sub-173", "p@customer.example")
    assert before.status_code == 200, before.text
    res = await _put(entra_client, user, org, good())
    assert res.status_code == 200, res.text
    after = await _call(entra_client, rsa_keypair, "entra-sub-173", "p@customer.example")
    assert after.content == before.content


async def test_s12_help_lists_the_route(client):
    res = await client.get("/api/v1/help")
    assert res.status_code == 200
    listed = {(e["method"], e["path"]) for e in res.json()["endpoints"]["authenticated"]}
    assert ("PUT", f"{get_settings().API_V1_PREFIX}/me/signup-profile") in listed


