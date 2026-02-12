from __future__ import annotations

from datetime import timedelta
import jwt

from app.core.config import Settings
from app.utils.time import utcnow


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
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, int(settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


def decode_access_token(settings: Settings, token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
