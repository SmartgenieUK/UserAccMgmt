from __future__ import annotations

from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    APP_NAME: str = "Account & Identity Platform"
    ENV: str = "dev"
    API_V1_PREFIX: str = "/api/v1"
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    SECRET_KEY: str = Field(..., min_length=32)
    JWT_ALGORITHM: str = "HS256"
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

    SMTP_HOST: str
    SMTP_PORT: int = 587
    SMTP_USER: str
    SMTP_PASSWORD: str
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str

    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None
    MICROSOFT_CLIENT_ID: str | None = None
    MICROSOFT_CLIENT_SECRET: str | None = None
    MICROSOFT_REDIRECT_URI: str | None = None
    OAUTH_PROVIDERS_ENABLED: list[str] = ["google", "microsoft"]
    OAUTH_STATE_TTL_SECONDS: int = 600

    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_REGISTER_PER_HOUR: int = 5
    RATE_LIMIT_RESET_PER_HOUR: int = 5
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = 120

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
