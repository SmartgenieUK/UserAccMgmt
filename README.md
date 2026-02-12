# Account & Identity Platform

Production-grade, reusable account and identity service built with FastAPI, PostgreSQL, Redis, and SQLAlchemy 2.0 async.

## Features
- Email/password registration with verification
- OAuth/OIDC for Google and Microsoft Entra ID
- Refresh token rotation with hashed tokens
- Multi-tenant organizations, invitations, memberships
- RBAC with scopes
- Rate limiting + lockout policy
- Structured logging + correlation IDs
- Audit logs
- Health and readiness endpoints
- Extensible hooks and plugin registry
- Python SDK and React integration example

## Quick Start
```bash
cp .env.example .env
docker-compose up --build
```

Run migrations:
```bash
docker-compose exec api alembic upgrade head
```

Open API docs:
```text
http://localhost:8000/api/v1/docs
```

## Integration Guide

### Register and verify
- POST `/api/v1/register`
- POST `/api/v1/verify-email`

### Login and refresh
- POST `/api/v1/login`
- POST `/api/v1/refresh`

### Organization flows
- POST `/api/v1/orgs`
- GET `/api/v1/orgs`
- POST `/api/v1/orgs/{id}/invite`
- POST `/api/v1/invitations/accept`

### Admin flows
- GET `/api/v1/admin/users`
- PATCH `/api/v1/admin/users/{id}/disable`

## SDK Usage (Python)
```python
from sdk import AuthClient

client = AuthClient("http://localhost:8000")
client.register("user@example.com", "StrongPass1!")
client.login("user@example.com", "StrongPass1!")
profile = client.get_me()
```

## OAuth
1. Call `GET /api/v1/oauth/{provider}/authorize` to get authorization URL.
2. Redirect user to provider.
3. Call `POST /api/v1/oauth/{provider}/callback` with `code` and `state`.

Providers enabled by env:
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
- `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_REDIRECT_URI`

## Hooks and Plugins
Use `HOOK_MODULES` to register custom password rules or validation hooks.
Use `PLUGIN_MODULES` to register additional OAuth providers or MFA modules.

## Testing
```bash
pytest
```

## Metrics
If enabled, Prometheus metrics are exposed on `/metrics`.
