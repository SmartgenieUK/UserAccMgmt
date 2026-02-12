from __future__ import annotations

from functools import lru_cache
from fastapi import Request

from app.core.config import get_settings
from app.core.hooks import HookManager, default_password_policy, default_email_domain_policy
from app.core.plugins import PluginRegistry, load_plugins
from app.db.redis import get_redis
from app.services.rate_limit_service import RateLimiter
from app.utils.profile_schema import ProfileSchemaRegistry, default_profile_registry


@lru_cache
def get_hooks() -> HookManager:
    settings = get_settings()
    hooks = HookManager(settings)
    hooks.register_password_policy_hook(default_password_policy(settings))
    hooks.register_email_domain_hook(default_email_domain_policy(settings))
    for module_path in settings.HOOK_MODULES:
        module = __import__(module_path, fromlist=["register"])
        if hasattr(module, "register"):
            module.register(hooks)
    return hooks


@lru_cache
def get_registry() -> PluginRegistry:
    settings = get_settings()
    registry = PluginRegistry()
    load_plugins(settings, registry)
    return registry


@lru_cache
def get_profile_registry() -> ProfileSchemaRegistry:
    return default_profile_registry()


def rate_limit_dependency(limit: int, period_seconds: int, key_prefix: str):
    async def _dep(request: Request):
        limiter = RateLimiter(get_redis(request))
        ip = request.client.host if request.client else "unknown"
        await limiter.hit(f"{key_prefix}:{ip}", limit, period_seconds)

    return _dep
