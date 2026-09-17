from __future__ import annotations

import types
import pytest

from app.core.plugins import PluginRegistry
from app.services.oauth_providers import EntraExternalProvider


async def test_oauth_entra_external_provider():
    settings = types.SimpleNamespace(
        ENTRA_TENANT_DOMAIN="testtenant",
        ENTRA_TENANT_ID="tenant-123-abc",
        ENTRA_CLIENT_ID="client-456-def",
        ENTRA_CLIENT_SECRET="secret-789-ghi",
    )
    registry = PluginRegistry()
    provider = EntraExternalProvider(settings)  # type: ignore[arg-type]
    registry.register_oauth_provider(provider)

    retrieved = registry.get_oauth_provider("entra_external")
    assert isinstance(retrieved, EntraExternalProvider)
    assert retrieved is provider
    assert retrieved.name == "entra_external"
    assert retrieved.client_id == "client-456-def"
    assert retrieved.client_secret == "secret-789-ghi"

    url = await provider.authorization_url(state="s", redirect_uri="http://cb", code_challenge="c")
    assert isinstance(url, str)
    assert "ciamlogin.com" in url
    assert "tenant-123-abc" in url
    assert "code_challenge_method=S256" in url
