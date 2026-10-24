"""CR-200 (BRIEF-048): pin today's framework behaviour so a dependency bump cannot change it unnoticed.

The 2026-10-21 certification of the FastAPI 0.133 / Starlette 1.3 upgrade (BRIEF-028) found three behaviours no
test held: FastAPI now refuses a JSON body sent without ``Content-Type: application/json``, the CORS policy, and the
cookies UAM sets (none). One test per behaviour; each was shown to fail under an in-memory mutant of its behaviour.
"""

from __future__ import annotations

import pytest
from starlette.middleware.cors import CORSMiddleware

from app.main import app

EMAIL = "pins@example.com"
PASSWORD = "StrongPass1!"
BODY = b'{"email":"nobody@example.com","password":"x"}'


def _cors_entry():
    return next((m for m in app.user_middleware if m.cls is CORSMiddleware), None)


async def test_json_body_without_content_type_is_refused(client):
    refused = await client.post("/api/v1/login", content=BODY)
    assert refused.status_code == 422, refused.text
    # Control: the same bytes declared as JSON reach the handler, which refuses the unknown user.
    reached = await client.post("/api/v1/login", content=BODY, headers={"content-type": "application/json"})
    assert reached.status_code == 401, reached.text


async def test_cors_refuses_an_unlisted_origin(client):
    entry = _cors_entry()
    assert entry is not None, "CORSMiddleware is not configured"
    origins = entry.kwargs["allow_origins"]
    assert "*" not in origins and "https://evil.example" not in origins, origins
    preflight = await client.options(
        "/api/v1/login",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers, dict(preflight.headers)
    post = await client.post(
        "/api/v1/login", content=BODY, headers={"origin": "https://evil.example", "content-type": "application/json"}
    )
    assert "access-control-allow-origin" not in post.headers, dict(post.headers)


@pytest.fixture()
def portal_origin_listed():
    """List one origin on UAM's real CORS layer for the test, and put the configured list back afterwards."""
    entry = _cors_entry()
    original = None if entry is None else entry.kwargs["allow_origins"]
    if entry is not None:
        entry.kwargs["allow_origins"] = ["https://portal.example"]
    app.middleware_stack = None
    try:
        yield
    finally:
        if entry is not None:
            entry.kwargs["allow_origins"] = original
        app.middleware_stack = None


async def test_cors_allows_a_listed_origin_with_credentials(portal_origin_listed, client):
    allowed = await client.options(
        "/api/v1/login",
        headers={"origin": "https://portal.example", "access-control-request-method": "POST"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "https://portal.example", dict(allowed.headers)
    assert allowed.headers.get("access-control-allow-credentials") == "true", dict(allowed.headers)
    # Control (the refused-origin pin is the test above).
    other = await client.options(
        "/api/v1/login",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in other.headers, dict(other.headers)


async def test_no_response_sets_a_cookie(client, sent_otps):
    responses = []
    register = await client.post("/api/v1/register", json={"email": EMAIL, "password": PASSWORD})
    assert register.status_code == 201, register.text
    responses.append(register)
    verify = await client.post("/api/v1/verify-email", json={"email": EMAIL, "otp": sent_otps[0]})
    assert verify.status_code == 200, verify.text
    responses.append(verify)
    login = await client.post("/api/v1/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    responses.append(login)
    refreshed = await client.post("/api/v1/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    responses.append(refreshed)
    logout = await client.post("/api/v1/logout", json={"refresh_token": refreshed.json()["refresh_token"]})
    assert logout.status_code == 200, logout.text
    responses.append(logout)
    for r in responses:
        assert "set-cookie" not in r.headers, (r.request.url.path, r.headers.get("set-cookie"))
