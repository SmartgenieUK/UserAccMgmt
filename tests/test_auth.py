from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_login_flow(client):
    register = await client.post("/api/v1/register", json={"email": "test@example.com", "password": "StrongPass1!"})
    assert register.status_code == 201

    verify = await client.post("/api/v1/verify-email", json={"token": "invalid"})
    assert verify.status_code in (400, 422)

    login = await client.post("/api/v1/login", json={"email": "test@example.com", "password": "StrongPass1!"})
    assert login.status_code in (401, 400)
