from __future__ import annotations

from datetime import timedelta
import jwt

from app.core.config import Settings
from app.security.keys import get_keyring
from app.utils.time import utcnow


def issuer(settings: Settings) -> str:
    return settings.JWT_ISSUER or settings.PUBLIC_BASE_URL


def _encode(settings: Settings, payload: dict) -> str:
    if settings.JWT_ALGORITHM == "RS256":
        ring = get_keyring(settings)
        payload = {**payload, "iss": issuer(settings), "aud": settings.JWT_AUDIENCE}
        return jwt.encode(payload, ring.private_key, algorithm="RS256", headers={"kid": ring.kid})
    # Legacy HS256 (pre-cutover): no iss/aud, so consumers that do not yet pass audience= keep working.
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_access_token(
    settings: Settings,
    subject: str,
    email: str,
    role: str,
    org_id: str,
    scopes: list[str],
) -> tuple[str, int]:
    now = utcnow()
    expires = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": subject,
        "email": email,
        "role": role,
        "org_id": org_id,
        "scopes": scopes,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return _encode(settings, payload), int(settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


def create_client_access_token(
    settings: Settings,
    client_id: str,
    org_id: str,
    scopes: list[str],
) -> tuple[str, int]:
    now = utcnow()
    expires = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": client_id,
        "org_id": org_id,
        "scopes": scopes,
        "token_type": "client",
        "client_id": client_id,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return _encode(settings, payload), int(settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


def decode_access_token(settings: Settings, token: str) -> dict:
    if settings.JWT_ALGORITHM == "RS256":
        ring = get_keyring(settings)
        kid = jwt.get_unverified_header(token).get("kid")
        # RS256 only — never list HS256 alongside it (algorithm confusion).
        return jwt.decode(
            token,
            ring.public_key(kid),
            algorithms=["RS256"],
            audience=settings.JWT_AUDIENCE,
            issuer=issuer(settings),
        )
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
