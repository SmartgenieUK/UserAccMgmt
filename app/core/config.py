from __future__ import annotations

from functools import lru_cache
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    APP_NAME: str = "Account & Identity Platform"
    ENV: str = "dev"
    API_V1_PREFIX: str = "/api/v1"
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    SECRET_KEY: str = Field(..., min_length=32)
    # HS256 = legacy shared-secret tokens (default until the CloudGenie cutover); RS256 = signed with
    # JWT_PRIVATE_KEY_PEM, carrying iss/aud/kid, verifiable via /.well-known/jwks.json.
    JWT_ALGORITHM: str = "HS256"
    JWT_PRIVATE_KEY_PEM: str | None = None
    JWT_KID: str = "uam-1"
    JWT_PREVIOUS_PUBLIC_KEY_PEM: str | None = None
    JWT_PREVIOUS_KID: str | None = None
    JWT_ISSUER: str | None = None  # defaults to PUBLIC_BASE_URL
    JWT_AUDIENCE: str = "uam"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    EMAIL_VERIFY_EXPIRE_HOURS: int = 24
    PASSWORD_RESET_EXPIRE_HOURS: int = 2
    EMAIL_CHANGE_EXPIRE_HOURS: int = 2

    EMAIL_VERIFY_PATH: str = "/verify-email"
    PASSWORD_RESET_PATH: str = "/reset-password"
    EMAIL_CHANGE_PATH: str = "/confirm-email"

    DATABASE_URL: str
    REDIS_URL: str | None = None
    REDIS_REQUIRED: bool = True

    ALLOWED_ORIGINS: list[str] = []

    USE_COOKIE_AUTH: bool = False
    USE_SECURE_COOKIES: bool = True
    COOKIE_DOMAIN: str | None = None
    COOKIE_SAMESITE: str = "lax"
    COOKIE_NAME_REFRESH: str = "refresh_token"
    COOKIE_NAME_CSRF: str = "csrf_token"

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = "DoNotReply@7ec7c562-c70c-4a9b-a549-6527452b67c2.azurecomm.net"

    EMAIL_PROVIDER: str = "acs"  # "smtp" or "acs"
    ACS_CONNECTION_STRING: str = ""
    OTP_LENGTH: int = 6
    OTP_EXPIRE_MINUTES: int = 10

    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    MICROSOFT_CLIENT_ID: str | None = None
    MICROSOFT_CLIENT_SECRET: str | None = None
    MICROSOFT_REDIRECT_URI: str | None = None
    OAUTH_PROVIDERS_ENABLED: list[str] = ["google", "microsoft"]
    OAUTH_STATE_TTL_SECONDS: int = 600

    # Entra custom-claims-provider (W3). ENTRA_ISSUER selects the tenant type as config, not code:
    # workforce = https://login.microsoftonline.com/{tenant}/v2.0, external = https://<dom>.ciamlogin.com/{tenant}/v2.0.
    ENTRA_TENANT_ID: str | None = None
    ENTRA_TENANT_DOMAIN: str | None = None
    ENTRA_CLIENT_ID: str | None = None
    ENTRA_CLIENT_SECRET: str | None = None
    ENTRA_ISSUER: str | None = None
    ENTRA_EXTENSION_APP_ID: str | None = None  # aud the inbound callout token must carry
    ENTRA_JWKS_URL: str | None = None  # production: Entra's signing keys, fetched + cached by PyJWKClient
    ENTRA_SIGNING_PUBLIC_KEY_PEM: str | None = None  # pin the key (no outbound fetch) / test seam
    ENTRA_PRODUCT_APP_IDS: dict[str, str] = {}  # calling client appId -> product

    STRIPE_API_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_MAP: dict[str, str] = {}
    STRIPE_CHECKOUT_SUCCESS_URL: str = ""
    STRIPE_CHECKOUT_CANCEL_URL: str = ""
    STRIPE_PORTAL_RETURN_URL: str = ""

    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_REGISTER_PER_HOUR: int = 5
    RATE_LIMIT_RESET_PER_HOUR: int = 5
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 60
    RATE_LIMIT_EMAIL_PER_RECIPIENT_PER_HOUR: int = 3

    LOCKOUT_THRESHOLD: int = 5
    LOCKOUT_DURATION_MINUTES: int = 15

    PASSWORD_MIN_LENGTH: int = 12
    PASSWORD_MAX_LENGTH: int = 128
    PASSWORD_REQUIRE_UPPER: bool = True
    PASSWORD_REQUIRE_LOWER: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True
    PASSWORD_REQUIRE_SPECIAL: bool = True

    ALLOWED_EMAIL_DOMAINS: list[str] = []

    AUDIT_LOG_ENABLED: bool = True
    METRICS_ENABLED: bool = True
    LOG_LEVEL: str = "INFO"

    PLUGIN_MODULES: list[str] = []
    HOOK_MODULES: list[str] = []

    PROFILE_SCHEMA_VERSION: int = 1

    @field_validator(
        "ALLOWED_ORIGINS",
        "OAUTH_PROVIDERS_ENABLED",
        "ALLOWED_EMAIL_DOMAINS",
        "PLUGIN_MODULES",
        "HOOK_MODULES",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ENTRA_PRODUCT_APP_IDS", mode="before")
    @classmethod
    def _parse_appid_map(cls, value):
        # Accept a JSON object, an "appId=product,appId2=product2" string, or a dict.
        if value is None:
            return {}
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return {}
            if value.startswith("{"):
                import json

                return json.loads(value)
            result = {}
            for pair in value.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if "=" not in pair:
                    # Fail loud with a clear message rather than a cryptic dict() ValueError on a
                    # malformed entry (review finding 2026-09-14).
                    raise ValueError(f"ENTRA_PRODUCT_APP_IDS entry '{pair}' is not appId=product")
                app_id, product = pair.split("=", 1)
                result[app_id.strip()] = product.strip()
            return result
        return value

    @field_validator("STRIPE_PRICE_MAP", mode="before")
    @classmethod
    def _parse_price_map(cls, value):
        if value is None:
            return {}
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return {}
            if value.startswith("{"):
                import json

                return json.loads(value)
            result = {}
            for pair in value.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if "=" not in pair:
                    raise ValueError(f"STRIPE_PRICE_MAP entry '{pair}' is not priceId=product:tier")
                price_id, product_tier = pair.split("=", 1)
                result[price_id.strip()] = product_tier.strip()
            return result
        return value

    @model_validator(mode="after")
    def _check_jwt(self):
        if self.JWT_ALGORITHM not in ("HS256", "RS256"):
            raise ValueError("JWT_ALGORITHM must be HS256 or RS256")
        if self.JWT_ALGORITHM == "RS256" and not self.JWT_PRIVATE_KEY_PEM:
            raise ValueError("JWT_PRIVATE_KEY_PEM is required when JWT_ALGORITHM=RS256")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
