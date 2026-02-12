from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from app.core.config import Settings
from app.core.exceptions import ValidationError


def create_csrf_token(settings: Settings) -> str:
    raw = secrets.token_urlsafe(32)
    sig = hmac.new(settings.SECRET_KEY.encode(), raw.encode(), sha256).hexdigest()
    return f"{raw}.{sig}"


def validate_csrf_token(settings: Settings, token: str) -> None:
    if "." not in token:
        raise ValidationError("Invalid CSRF token", code="csrf_invalid")
    raw, sig = token.split(".", 1)
    expected = hmac.new(settings.SECRET_KEY.encode(), raw.encode(), sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValidationError("Invalid CSRF token", code="csrf_invalid")
