from __future__ import annotations

import importlib
from typing import Protocol, Any

from app.core.config import Settings
from app.core.exceptions import ValidationError


class OAuthProvider(Protocol):
    name: str

    async def authorization_url(self, state: str, redirect_uri: str, code_challenge: str | None) -> str: ...
    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict: ...
    async def fetch_user_info(self, token_data: dict) -> Any: ...


class MFAModule(Protocol):
    name: str

    async def initiate(self, user_id: str) -> dict: ...
    async def verify(self, user_id: str, payload: dict) -> bool: ...


class PluginRegistry:
    def __init__(self):
        self._oauth_providers: dict[str, OAuthProvider] = {}
        self._mfa_modules: dict[str, MFAModule] = {}

    def register_oauth_provider(self, provider: OAuthProvider) -> None:
        self._oauth_providers[provider.name] = provider

    def register_mfa_module(self, module: MFAModule) -> None:
        self._mfa_modules[module.name] = module

    def get_oauth_provider(self, name: str) -> OAuthProvider:
        provider = self._oauth_providers.get(name)
        if not provider:
            raise ValidationError("OAuth provider not available", code="oauth_provider_unavailable")
        return provider

    def get_mfa_module(self, name: str) -> MFAModule:
        module = self._mfa_modules.get(name)
        if not module:
            raise ValidationError("MFA module not available", code="mfa_module_unavailable")
        return module

    @property
    def oauth_providers(self) -> dict[str, OAuthProvider]:
        return dict(self._oauth_providers)


def load_plugins(settings: Settings, registry: PluginRegistry) -> None:
    for module_path in settings.PLUGIN_MODULES:
        module = importlib.import_module(module_path)
        if hasattr(module, "register"):
            module.register(registry)
