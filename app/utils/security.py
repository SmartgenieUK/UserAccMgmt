from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Tuple


def normalize_email(email: str) -> str:
    return email.strip().lower()


def generate_token_secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def split_token(token: str) -> Tuple[str, str]:
    if "." not in token:
        raise ValueError("Invalid token format")
    return token.split(".", 1)


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
