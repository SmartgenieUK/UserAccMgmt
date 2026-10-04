from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings, get_settings
from app.main import app
from app.security.jwt import create_access_token, create_client_access_token, decode_access_token

SCOPES = ["profile:read"]


def _mint(settings: Settings) -> str:
    token, _ = create_access_token(settings, subject="user-1", email="a@b.c", role="admin", org_id="org-1", scopes=SCOPES)
    return token


@pytest.fixture()
def rs_settings(rsa_keypair) -> Settings:
    private_pem, _ = rsa_keypair
    return Settings(JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=private_pem.decode(), JWT_KID="test-1")


@pytest.fixture()
def hs_settings() -> Settings:
    return Settings()


def test_rs256_roundtrip_carries_iss_aud_kid(rs_settings):
    token = _mint(rs_settings)
    assert jwt.get_unverified_header(token) == {"alg": "RS256", "typ": "JWT", "kid": "test-1"}
    claims = decode_access_token(rs_settings, token)
    assert claims["sub"] == "user-1"
    assert claims["iss"] == rs_settings.PUBLIC_BASE_URL
    assert claims["aud"] == "uam"
    assert claims["scopes"] == SCOPES


def test_client_token_is_signed_the_same_way(rs_settings):
    token, _ = create_client_access_token(rs_settings, client_id="cid", org_id="org-1", scopes=SCOPES)
    claims = decode_access_token(rs_settings, token)
    assert claims["token_type"] == "client" and claims["aud"] == "uam"


def test_wrong_audience_rejected(rs_settings, rsa_keypair):
    other = Settings(JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=rsa_keypair[0].decode(), JWT_KID="test-1", JWT_AUDIENCE="other")
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(rs_settings, _mint(other))


def test_wrong_issuer_rejected(rs_settings, rsa_keypair):
    other = Settings(JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=rsa_keypair[0].decode(), JWT_KID="test-1", JWT_ISSUER="https://evil")
    with pytest.raises(jwt.InvalidIssuerError):
        decode_access_token(rs_settings, _mint(other))


def test_hs256_token_rejected_in_rs256_mode(rs_settings, hs_settings):
    # A token signed with the shared secret must not verify once RS256 is on — even with the right secret.
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(rs_settings, _mint(hs_settings))


def test_alg_none_rejected(rs_settings):
    unsigned = jwt.encode({"sub": "user-1", "iss": rs_settings.PUBLIC_BASE_URL, "aud": "uam"}, key=None, algorithm="none", headers={"kid": "test-1"})
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(rs_settings, unsigned)


def test_unknown_kid_rejected(rs_settings, rsa_keypair):
    other = Settings(JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=rsa_keypair[0].decode(), JWT_KID="not-published")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(rs_settings, _mint(other))


def test_previous_key_still_verifies_during_rotation(rsa_keypair):
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_private = old_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    old_public = old_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    old = Settings(JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=old_private, JWT_KID="old")
    rotated = Settings(
        JWT_ALGORITHM="RS256", JWT_PRIVATE_KEY_PEM=rsa_keypair[0].decode(), JWT_KID="new",
        JWT_PREVIOUS_PUBLIC_KEY_PEM=old_public, JWT_PREVIOUS_KID="old",
    )
    assert decode_access_token(rotated, _mint(old))["sub"] == "user-1"
    assert jwt.get_unverified_header(_mint(rotated))["kid"] == "new"


def test_hs256_mode_is_byte_for_byte_legacy(hs_settings):
    token = _mint(hs_settings)
    assert jwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}
    claims = decode_access_token(hs_settings, token)
    assert "iss" not in claims and "aud" not in claims


def test_rs256_requires_private_key():
    with pytest.raises(ValueError):
        Settings(JWT_ALGORITHM="RS256")


async def test_jwks_and_discovery_endpoints(client, rs_settings, rsa_keypair):
    app.dependency_overrides[get_settings] = lambda: rs_settings
    try:
        jwks = (await client.get("/.well-known/jwks.json")).json()
        assert [k["kid"] for k in jwks["keys"]] == ["test-1"]
        assert jwks["keys"][0]["kty"] == "RSA" and jwks["keys"][0]["alg"] == "RS256" and "d" not in jwks["keys"][0]
        # The published key verifies what the server minted.
        public = jwt.PyJWK(jwks["keys"][0]).key
        assert jwt.decode(_mint(rs_settings), public, algorithms=["RS256"], audience="uam")["sub"] == "user-1"

        disco = (await client.get("/.well-known/openid-configuration")).json()
        assert disco["issuer"] == rs_settings.PUBLIC_BASE_URL
        assert disco["jwks_uri"].endswith("/.well-known/jwks.json")
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def test_jwks_is_empty_without_a_key(client):
    assert (await client.get("/.well-known/jwks.json")).json() == {"keys": []}


async def test_rs256_token_passes_the_route_guard(rs_settings):
    # UAM's own guarded routes (/me, /orgs) must accept the iss/aud an RS256 token carries.
    from fastapi.security import HTTPAuthorizationCredentials
    from app.security.dependencies import get_token_payload

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_mint(rs_settings))
    payload = await get_token_payload(creds, rs_settings)
    assert payload.sub == "user-1" and payload.scopes == SCOPES
