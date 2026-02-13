from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import User, Credential, Membership, VerificationToken
from app.models.enums import VerificationTokenType


@pytest.mark.asyncio
async def test_register_login_flow(client, db_session):
    register = await client.post("/api/v1/register", json={"email": "test@example.com", "password": "StrongPass1!"})
    assert register.status_code == 201

    user_result = await db_session.execute(select(User).where(User.normalized_email == "test@example.com"))
    user = user_result.scalar_one_or_none()
    assert user is not None

    credential_result = await db_session.execute(select(Credential).where(Credential.user_id == user.id))
    assert credential_result.scalar_one_or_none() is not None

    membership_result = await db_session.execute(select(Membership).where(Membership.user_id == user.id))
    assert membership_result.scalar_one_or_none() is not None

    verification_result = await db_session.execute(
        select(VerificationToken).where(
            VerificationToken.user_id == user.id,
            VerificationToken.token_type == VerificationTokenType.EMAIL_VERIFY,
        )
    )
    assert verification_result.scalar_one_or_none() is not None

    verify = await client.post("/api/v1/verify-email", json={"token": "invalid"})
    assert verify.status_code in (400, 422)

    login = await client.post("/api/v1/login", json={"email": "test@example.com", "password": "StrongPass1!"})
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "email_not_verified"
