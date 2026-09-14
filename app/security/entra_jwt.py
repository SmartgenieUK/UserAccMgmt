from __future__ import annotations

from functools import lru_cache

import jwt

from app.core.config import Settings
from app.core.exceptions import AppError, AuthError

# The Microsoft "Azure AD authentication events" service principal. Every custom-authentication-extension
# callout is bearer-authenticated with a token whose azp (v2) / appid (v1) is exactly this. A token from
# any other caller is rejected even if iss/aud/signature check out. (Microsoft Learn, custom-claims-provider.)
ENTRA_AUTH_EVENTS_APP_ID = "99045fe1-7639-4a75-9d4a-577b6ca3810f"


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str):
    # Cached per URL so Entra's signing keys are fetched once, not per callout.
    return jwt.PyJWKClient(jwks_url)


def _signing_key(settings: Settings, token: str):
    if settings.ENTRA_SIGNING_PUBLIC_KEY_PEM:
        return settings.ENTRA_SIGNING_PUBLIC_KEY_PEM
    if settings.ENTRA_JWKS_URL:
        return _jwks_client(settings.ENTRA_JWKS_URL).get_signing_key_from_jwt(token).key
    raise AppError("Entra signing key not configured", status_code=500, code="entra_not_configured")


def verify_callout_token(settings: Settings, token: str) -> dict:
    """Verify the bearer token Entra presents on a token-issuance-start callout.

    RS256 only; enforces aud = our extension app registration, iss = the configured tenant issuer, and
    azp/appid = the authentication-events service. Any failure raises AuthError (-> 401), so a forged or
    misdirected callout can never reach the claims logic.
    """
    if not settings.ENTRA_ISSUER or not settings.ENTRA_EXTENSION_APP_ID:
        raise AppError("Entra issuer/audience not configured", status_code=500, code="entra_not_configured")
    try:
        claims = jwt.decode(
            token,
            _signing_key(settings, token),
            algorithms=["RS256"],
            audience=settings.ENTRA_EXTENSION_APP_ID,
            issuer=settings.ENTRA_ISSUER,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
        raise AuthError("Invalid Entra callout token", code="entra_token_invalid") from exc
    azp = claims.get("azp") or claims.get("appid")
    if azp != ENTRA_AUTH_EVENTS_APP_ID:
        raise AuthError("Callout not from the authentication-events service", code="entra_azp_invalid")
    return claims
