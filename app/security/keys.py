from __future__ import annotations

from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings


class KeyRing:
    """RS256 signing material: one private key (active kid) plus optional previous public key for rotation.

    Only the active key signs. Both kids verify, so tokens minted before a rotation stay valid for their
    15-minute life while consumers refresh the JWKS. PEMs may arrive from env with literal "\\n" escapes.
    """

    def __init__(self, private_pem: str, kid: str, previous_public_pem: str | None, previous_kid: str | None):
        self.kid = kid
        self.private_key = serialization.load_pem_private_key(_unescape(private_pem).encode(), password=None)
        self._public = {kid: self.private_key.public_key()}
        if previous_public_pem and previous_kid:
            self._public[previous_kid] = serialization.load_pem_public_key(_unescape(previous_public_pem).encode())

    def public_key(self, kid: str | None):
        try:
            return self._public[kid]
        except KeyError:
            raise jwt.InvalidTokenError("Unknown signing key")

    def jwks(self) -> dict:
        return {
            "keys": [
                {**RSAAlgorithm.to_jwk(key, as_dict=True), "kid": kid, "use": "sig", "alg": "RS256"}
                for kid, key in self._public.items()
            ]
        }


def _unescape(pem: str) -> str:
    return pem.replace("\\n", "\n")


@lru_cache(maxsize=4)
def _build(private_pem: str, kid: str, previous_public_pem: str | None, previous_kid: str | None) -> KeyRing:
    return KeyRing(private_pem, kid, previous_public_pem, previous_kid)


def get_keyring(settings: Settings) -> KeyRing | None:
    if not settings.JWT_PRIVATE_KEY_PEM:
        return None
    return _build(settings.JWT_PRIVATE_KEY_PEM, settings.JWT_KID, settings.JWT_PREVIOUS_PUBLIC_KEY_PEM, settings.JWT_PREVIOUS_KID)
