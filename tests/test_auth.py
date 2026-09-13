from __future__ import annotations

from datetime import timedelta

import jwt
from sqlalchemy import select

from app.models import User, Credential, Membership, Organization, Role
from app.utils.time import utcnow

EMAIL = "test@example.com"
PASSWORD = "StrongPass1!"


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


async def _register_and_verify(client, sent_otps):
    register = await client.post("/api/v1/register", json={"email": EMAIL, "password": PASSWORD})
    assert register.status_code == 201, register.text
    assert len(sent_otps) == 1
    verify = await client.post("/api/v1/verify-email", json={"email": EMAIL, "otp": sent_otps[0]})
    assert verify.status_code == 200, verify.text


async def test_register_verify_login_flow(client, db_session, sent_otps):
    register = await client.post("/api/v1/register", json={"email": EMAIL, "password": PASSWORD})
    assert register.status_code == 201, register.text

    user = (await db_session.execute(select(User).where(User.normalized_email == EMAIL))).scalar_one()
    assert (await db_session.execute(select(Credential).where(Credential.user_id == user.id))).scalar_one_or_none()
    assert (await db_session.execute(select(Membership).where(Membership.user_id == user.id))).scalar_one_or_none()
    assert user.is_verified is False

    # Unverified login is refused; a wrong OTP does not verify.
    login = await client.post("/api/v1/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "email_not_verified"
    bad = await client.post("/api/v1/verify-email", json={"email": EMAIL, "otp": "000000"})
    assert bad.status_code in (400, 422)

    # The OTP the app "emailed" verifies, and it is single-use.
    assert len(sent_otps) == 1
    ok = await client.post("/api/v1/verify-email", json={"email": EMAIL, "otp": sent_otps[0]})
    assert ok.status_code == 200, ok.text
    again = await client.post("/api/v1/verify-email", json={"email": EMAIL, "otp": sent_otps[0]})
    assert again.status_code in (400, 422)

    login = await client.post("/api/v1/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["token_type"] == "bearer"
    claims = _claims(body["access_token"])
    assert claims["sub"] == str(user.id)
    assert claims["role"] == Role.ADMIN.value


async def test_multi_org_user_defaults_to_earliest_membership(client, db_session, sent_otps):
    """A user in two orgs logs in and refreshes without org_id (this used to raise MultipleResultsFound)."""
    await _register_and_verify(client, sent_otps)
    user = (await db_session.execute(select(User).where(User.normalized_email == EMAIL))).scalar_one()
    first_org_id = (
        await db_session.execute(select(Membership.org_id).where(Membership.user_id == user.id))
    ).scalar_one()

    second = Organization(name="Second Org", slug="second-org")
    db_session.add(second)
    await db_session.flush()
    # Explicit later created_at: sqlite's CURRENT_TIMESTAMP is second-resolution and would tie.
    db_session.add(
        Membership(user_id=user.id, org_id=second.id, role=Role.MEMBER, created_at=utcnow() + timedelta(minutes=1))
    )
    await db_session.commit()

    login = await client.post("/api/v1/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert _claims(login.json()["access_token"])["org_id"] == str(first_org_id)

    refreshed = await client.post("/api/v1/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert _claims(refreshed.json()["access_token"])["org_id"] == str(first_org_id)

    explicit = await client.post(
        "/api/v1/login", json={"email": EMAIL, "password": PASSWORD, "org_id": str(second.id)}
    )
    assert explicit.status_code == 200, explicit.text
    assert _claims(explicit.json()["access_token"])["org_id"] == str(second.id)
