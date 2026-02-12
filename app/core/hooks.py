from __future__ import annotations

import inspect
from typing import Callable, Awaitable, Any

from app.core.config import Settings
from app.core.exceptions import ValidationError


RegistrationHook = Callable[..., Awaitable[None] | None]
PasswordPolicyHook = Callable[[str], Awaitable[None] | None]
EmailDomainHook = Callable[[str], Awaitable[None] | None]
ProfileValidationHook = Callable[[dict[str, Any], int], Awaitable[dict[str, Any]] | dict[str, Any]]


class HookManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._registration_hooks: list[RegistrationHook] = []
        self._password_policy_hooks: list[PasswordPolicyHook] = []
        self._email_domain_hooks: list[EmailDomainHook] = []
        self._profile_validation_hooks: list[ProfileValidationHook] = []

    async def _run(self, hook: Callable[..., Any], *args, **kwargs) -> Any:
        result = hook(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def register_registration_hook(self, hook: RegistrationHook) -> None:
        self._registration_hooks.append(hook)

    def register_password_policy_hook(self, hook: PasswordPolicyHook) -> None:
        self._password_policy_hooks.append(hook)

    def register_email_domain_hook(self, hook: EmailDomainHook) -> None:
        self._email_domain_hooks.append(hook)

    def register_profile_validation_hook(self, hook: ProfileValidationHook) -> None:
        self._profile_validation_hooks.append(hook)

    async def run_registration_hooks(self, email: str, password: str, display_name: str | None) -> None:
        for hook in self._registration_hooks:
            await self._run(hook, email=email, password=password, display_name=display_name)

    async def run_password_policy(self, password: str) -> None:
        for hook in self._password_policy_hooks:
            await self._run(hook, password)

    async def run_email_domain_checks(self, email: str) -> None:
        for hook in self._email_domain_hooks:
            await self._run(hook, email)

    async def run_profile_validation(self, data: dict[str, Any], version: int) -> dict[str, Any]:
        current = data
        for hook in self._profile_validation_hooks:
            current = await self._run(hook, current, version)
        return current


def default_password_policy(settings: Settings) -> PasswordPolicyHook:
    def _policy(password: str) -> None:
        if len(password) < settings.PASSWORD_MIN_LENGTH:
            raise ValidationError("Password too short", code="password_too_short")
        if len(password) > settings.PASSWORD_MAX_LENGTH:
            raise ValidationError("Password too long", code="password_too_long")
        if settings.PASSWORD_REQUIRE_UPPER and not any(c.isupper() for c in password):
            raise ValidationError("Password must include an uppercase letter", code="password_upper_required")
        if settings.PASSWORD_REQUIRE_LOWER and not any(c.islower() for c in password):
            raise ValidationError("Password must include a lowercase letter", code="password_lower_required")
        if settings.PASSWORD_REQUIRE_DIGIT and not any(c.isdigit() for c in password):
            raise ValidationError("Password must include a digit", code="password_digit_required")
        if settings.PASSWORD_REQUIRE_SPECIAL and not any(not c.isalnum() for c in password):
            raise ValidationError("Password must include a special character", code="password_special_required")

    return _policy


def default_email_domain_policy(settings: Settings) -> EmailDomainHook:
    def _policy(email: str) -> None:
        if not settings.ALLOWED_EMAIL_DOMAINS:
            return
        domain = email.split("@")[-1].lower()
        if domain not in [d.lower() for d in settings.ALLOWED_EMAIL_DOMAINS]:
            raise ValidationError("Email domain not allowed", code="email_domain_not_allowed")

    return _policy
