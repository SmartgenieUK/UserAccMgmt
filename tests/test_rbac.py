from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_admin_list_users_requires_scope(client):
    res = await client.get("/api/v1/admin/users")
    assert res.status_code == 401
