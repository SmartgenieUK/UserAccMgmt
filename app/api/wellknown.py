from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.security.jwt import issuer
from app.security.keys import get_keyring

router = APIRouter(include_in_schema=False)


@router.get("/.well-known/jwks.json")
async def jwks(settings=Depends(get_settings)):
    ring = get_keyring(settings)
    return ring.jwks() if ring else {"keys": []}


@router.get("/.well-known/openid-configuration")
async def openid_configuration(settings=Depends(get_settings)):
    iss = issuer(settings)
    return {
        "issuer": iss,
        "jwks_uri": f"{iss.rstrip('/')}/.well-known/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"] if get_keyring(settings) else [],
    }
