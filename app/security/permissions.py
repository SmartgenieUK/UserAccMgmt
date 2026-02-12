from __future__ import annotations

from app.models.enums import Role

ROLE_SCOPES: dict[Role, list[str]] = {
    Role.ADMIN: [
        "profile:read",
        "profile:write",
        "orgs:read",
        "orgs:write",
        "invitations:write",
        "users:read",
        "users:write",
        "admin:users:read",
        "admin:users:write",
    ],
    Role.MEMBER: ["profile:read", "profile:write", "orgs:read", "users:read"],
    Role.READONLY: ["profile:read", "orgs:read", "users:read"],
}


def resolve_scopes(role: Role) -> list[str]:
    return ROLE_SCOPES.get(role, [])
