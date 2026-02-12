from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_org_list_requires_auth(client):
    res = await client.get("/api/v1/orgs")
    assert res.status_code == 401
