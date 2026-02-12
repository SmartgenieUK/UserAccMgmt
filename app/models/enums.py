from __future__ import annotations
from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"
    READONLY = "readonly"


class VerificationTokenType(str, Enum):
    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"
    EMAIL_CHANGE = "email_change"


class ExternalProvider(str, Enum):
    GOOGLE = "google"
    MICROSOFT = "microsoft"
