from __future__ import annotations

from dataclasses import dataclass
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.core.config import Settings


@dataclass
class OAuthUserInfo:
    sub: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


class BaseOAuthProvider:
    name: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    scopes: list[str]

    def __init__(self, settings: Settings):
        self.settings = settings

    async def authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None) -> str:
        client = AsyncOAuth2Client(
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            scope=" ".join(self.scopes),
        )
        extra = {}
        if code_challenge:
            extra = {"code_challenge": code_challenge, "code_challenge_method": "S256"}
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
        return OAuthUserInfo(
            sub=str(data.get("sub") or data.get("id")),
            email=str(data.get("email") or data.get("userPrincipalName")),
            email_verified=bool(data.get("email_verified", True)),
            name=data.get("name"),
            picture=data.get("picture"),
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
