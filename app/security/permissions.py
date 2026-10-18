from __future__ import annotations

from app.models.enums import Role

# Held by no role: added to a user's token only when their id is in PLATFORM_OPERATOR_USER_IDS (BUG-06).
PLATFORM_ADMIN = "platform:admin"

ROLE_SCOPES: dict[Role, list[str]] = {
    Role.ADMIN: [
        "profile:read",
        "profile:write",
        "orgs:read",
        "orgs:write",
        "invitations:write",
        "users:read",
        "users:write",
        "apps:read",
        "apps:write",
        "entitlements:read",
        "billing:write",
    ],
    Role.MEMBER: ["profile:read", "profile:write", "orgs:read", "users:read", "apps:read", "entitlements:read"],
    Role.READONLY: ["profile:read", "orgs:read", "users:read", "apps:read", "entitlements:read"],
}


def resolve_scopes(role: Role) -> list[str]:
    return ROLE_SCOPES.get(role, [])
