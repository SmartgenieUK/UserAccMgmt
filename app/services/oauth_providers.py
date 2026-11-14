from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass

import jwt
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.core.config import Settings
from app.core.exceptions import AppError, AuthError


@dataclass
class OAuthUserInfo:
    sub: str  # the link key callers store as provider_user_id; for entra_external, the verified oid
    email: str | None  # None when the provider sent none; userPrincipalName is not an email
    email_verified: bool  # True only when the provider said literal boolean true (CR-208 certification F1)
    name: str | None
    picture: str | None
    first_name: str | None = None
    last_name: str | None = None
    role: str | None = None


def profile_hint(value, cap: int) -> str | None:
    """Provider hints are optional: bad types, controls and oversized values are discarded, never login failures."""
    if not isinstance(value, str) or any(unicodedata.category(ch) == "Cc" for ch in value):
        return None
    value = value.strip()
    return value if value and len(value) <= cap else None


def _prefill(data: dict) -> dict[str, str | None]:
    def hint(cap: int, *keys: str) -> str | None:
        for key in keys:
            value = profile_hint(data.get(key), cap)
            if value is not None:
                return value
        return None

    return {
        "first_name": hint(80, "given_name", "givenName"),
        "last_name": hint(80, "family_name", "surname"),
        "role": hint(120, "job_title", "jobTitle"),
    }


class BaseOAuthProvider:
    name: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    scopes: list[str]

    def __init__(self, settings: Settings):
        self.settings = settings

    async def authorization_url(
        self, state: str, redirect_uri: str, code_challenge: str | None, domain_hint: str | None = None
    ) -> str:
        client = AsyncOAuth2Client(
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            scope=" ".join(self.scopes),
        )
        extra = {}
        if code_challenge:
            extra = {"code_challenge": code_challenge, "code_challenge_method": "S256"}
        if domain_hint:  # standard OAuth parameter; the caller has already allow-listed it
            extra["domain_hint"] = domain_hint
        url, _ = client.create_authorization_url(self.authorization_endpoint, state=state, **extra)
        await client.aclose()
        return url

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        client = AsyncOAuth2Client(client_id=self.client_id, client_secret=self.client_secret)
        token = await client.fetch_token(
            self.token_endpoint,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        await client.aclose()
        return token

    async def fetch_user_info(self, token_data: dict) -> OAuthUserInfo:
        client = AsyncOAuth2Client(client_id=self.client_id, token=token_data)
        resp = await client.get(self.userinfo_endpoint)
        resp.raise_for_status()
        data = resp.json()
        await client.aclose()
        hints = _prefill(data)
        name = profile_hint(data.get("name"), 160) or profile_hint(data.get("displayName"), 160)
        # Entra combines these hints with verified claims before considering a display-name fallback.
        if self.name != "entra_external" and not hints["first_name"] and not hints["last_name"]:
            hints["first_name"] = profile_hint(name, 80)
        return OAuthUserInfo(
            sub=str(data.get("sub") or data.get("id")),
            email=data.get("email") if isinstance(data.get("email"), str) and data.get("email") else None,
            email_verified=data.get("email_verified") is True,
            name=name,
            picture=data.get("picture"),
            **hints,
        )

    @property
    def client_id(self) -> str:
        raise NotImplementedError

    @property
    def client_secret(self) -> str:
        raise NotImplementedError


class GoogleProvider(BaseOAuthProvider):
    name = "google"
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    scopes = ["openid", "email", "profile"]

    @property
    def client_id(self) -> str:
        return self.settings.GOOGLE_CLIENT_ID or ""

    @property
    def client_secret(self) -> str:
        return self.settings.GOOGLE_CLIENT_SECRET or ""


class MicrosoftProvider(BaseOAuthProvider):
    name = "microsoft"
    authorization_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    userinfo_endpoint = "https://graph.microsoft.com/oidc/userinfo"
    scopes = ["openid", "email", "profile"]

    @property
    def client_id(self) -> str:
        return self.settings.MICROSOFT_CLIENT_ID or ""

    @property
    def client_secret(self) -> str:
        return self.settings.MICROSOFT_CLIENT_SECRET or ""


class EntraExternalProvider(BaseOAuthProvider):
    name = "entra_external"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        base = f"https://{settings.ENTRA_TENANT_DOMAIN}.ciamlogin.com/{settings.ENTRA_TENANT_ID}/oauth2/v2.0"
        self.authorization_endpoint = f"{base}/authorize"
        self.token_endpoint = f"{base}/token"
        self.userinfo_endpoint = "https://graph.microsoft.com/oidc/userinfo"
        self.scopes = ["openid", "email", "profile"]
        self._jwks = None

    @property
    def client_id(self) -> str:
        return self.settings.ENTRA_CLIENT_ID or ""

    @property
    def client_secret(self) -> str:
        return self.settings.ENTRA_CLIENT_SECRET or ""

    async def fetch_user_info(self, token_data: dict) -> OAuthUserInfo:
        """Link by the VERIFIED ID token's `oid` (CR-208 work item B).

        Graph userinfo's `sub` is pairwise — different for every application — so it cannot be the key the
        claims callout and DevGenie's lookup share. `oid` is the user's object id, the same across the
        tenant's applications. The ID token from the code exchange is verified (RS256 signature, issuer,
        audience = this client, expiry) and its `sub` must equal userinfo's, binding the two responses to one
        sign-in. Any failure is an AuthError; nothing is decoded unverified.
        """
        claims = await self._verify_id_token(token_data.get("id_token"))
        info = await super().fetch_user_info(token_data)
        if info.sub != claims.get("sub"):
            raise AuthError("ID token and userinfo disagree", code="oauth_id_token_invalid")
        info.sub = claims["oid"]
        # Claims are used only after signature/issuer/audience/expiry verification and subject binding.
        hints = _prefill(claims)
        for field, value in hints.items():
            if getattr(info, field) is None:
                setattr(info, field, value)
        if not info.first_name and not info.last_name:
            info.first_name = (profile_hint(info.name, 80) or profile_hint(claims.get("name"), 80)
                               or profile_hint(claims.get("displayName"), 80))
        return info

    async def _verify_id_token(self, id_token) -> dict:
        if not self.settings.ENTRA_ISSUER or not self.client_id:
            raise AppError("Entra issuer/client not configured", status_code=500, code="entra_not_configured")
        if not isinstance(id_token, str) or not id_token:
            raise AuthError("No ID token from the provider", code="oauth_id_token_invalid")
        try:
            key = await self._signing_key(id_token)
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.settings.ENTRA_ISSUER,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
            raise AuthError("Invalid ID token", code="oauth_id_token_invalid") from exc
        if not isinstance(claims.get("oid"), str) or not claims["oid"]:
            raise AuthError("ID token carries no oid", code="oauth_id_token_invalid")
        return claims

    async def _signing_key(self, id_token: str):
        # A pinned key wins (tests, or no outbound fetch). Otherwise the tenant's JWKS through PyJWKClient,
        # which caches keys; the fetch runs off the event loop. Portal sign-in is not the Tier-0 callout path,
        # so a cached JWKS fetch here is acceptable.
        if self.settings.ENTRA_SIGNING_PUBLIC_KEY_PEM:
            return self.settings.ENTRA_SIGNING_PUBLIC_KEY_PEM
        if self.settings.ENTRA_JWKS_URL:
            if self._jwks is None:
                self._jwks = jwt.PyJWKClient(self.settings.ENTRA_JWKS_URL)
            return (await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, id_token)).key
        raise AppError("Entra signing key not configured", status_code=500, code="entra_not_configured")
