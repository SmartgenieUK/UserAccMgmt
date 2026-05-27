# Account & Identity Platform

Production-grade, reusable account and identity service built with FastAPI, PostgreSQL, Redis, and SQLAlchemy 2.0 async.

## Documentation
- See `docs/ARCHITECTURE_OVERVIEW.md` for system architecture, runtime flows, data model, and security design.
- See `docs/IAC_ARCHITECTURE.md` for Terraform/Azure infrastructure architecture, resource graph, variables, and operations.
- See `docs/OPERATIONAL_RUNBOOK.md` for day-2 operations, incident playbooks, rotations, and release/rollback procedures.
- See `docs/USER_GUIDE.md` for account lifecycle, organization workflows, admin actions, and API usage examples.
- See `docs/DEPLOYMENT_GUIDE.md` for local Docker deployment, Azure IaC provisioning, and Container Apps rollout.

## Features
- Email/password registration with OTP-based email verification
- Resend verification OTP for unverified accounts
- OAuth/OIDC login for Google and Microsoft Entra ID
- OAuth2 client credentials grant for machine-to-machine auth
- Per-org application registration (client_id/secret + scope allowlist)
- Refresh token rotation with hashed tokens
- Multi-tenant organizations, invitations, memberships
- RBAC with scopes
- Rate limiting (global + per-endpoint + per-email-recipient) and lockout policy
- Email domain allowlist + per-recipient rate limiting (abuse protection)
- SMTP and Azure Communication Services (ACS) email providers
- Structured logging + correlation IDs
- Audit logs
- Health, readiness, and machine-readable `/help` endpoints
- Extensible hooks and plugin registry
- Python SDK, React example, and interactive HTML test console

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
- POST `/api/v1/verify-email` — body: `{ "email", "otp" }`
- POST `/api/v1/resend-verification` — body: `{ "email" }`

### Login and refresh
- POST `/api/v1/login`
- POST `/api/v1/refresh`
- POST `/api/v1/logout`

### Client credentials (machine-to-machine)
- POST `/api/v1/auth/token` — body: `{ "client_id", "client_secret", "scopes": [...] }`

### Organization flows
- POST `/api/v1/orgs`
- GET `/api/v1/orgs`
- POST `/api/v1/orgs/{id}/invite`
- POST `/api/v1/invitations/accept`

### Application (OAuth2 client) management
- POST `/api/v1/orgs/{org_id}/apps` — returns `client_id` + `client_secret` (shown once)
- GET `/api/v1/orgs/{org_id}/apps`
- GET `/api/v1/orgs/{org_id}/apps/{app_id}`
- PATCH `/api/v1/orgs/{org_id}/apps/{app_id}`
- POST `/api/v1/orgs/{org_id}/apps/{app_id}/rotate-secret`
- DELETE `/api/v1/orgs/{org_id}/apps/{app_id}`

### Admin flows
- GET `/api/v1/admin/users`
- PATCH `/api/v1/admin/users/{id}/disable`

### Discovery
- GET `/api/v1/help` — machine-readable API reference (endpoints, scopes, roles, rate limits, password policy)

## SDK Usage (Python)
```python
from sdk import AuthClient

client = AuthClient("http://localhost:8000")
client.register("user@example.com", "StrongPass1!")
client.login("user@example.com", "StrongPass1!")
profile = client.get_me()
```

## OAuth (user login)
1. Call `GET /api/v1/oauth/{provider}/authorize` to get authorization URL.
2. Redirect user to provider.
3. Call `POST /api/v1/oauth/{provider}/callback` with `code` and `state`.

Providers enabled by env:
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
- `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_REDIRECT_URI`

## Client Credentials (machine-to-machine)
1. Organization admin registers an application via `POST /orgs/{org_id}/apps` and receives a `client_id` + `client_secret` (secret shown once).
2. Server application exchanges credentials for an access token:
   ```bash
   curl -X POST "http://localhost:8000/api/v1/auth/token" \
     -H "Content-Type: application/json" \
     -d '{"client_id":"...","client_secret":"...","scopes":["apps:read"]}'
   ```
3. Granted scopes are the intersection of the request and the application's `allowed_scopes`.

## Email Provider
Set `EMAIL_PROVIDER` to either:
- `smtp` — requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`
- `acs` — Azure Communication Services, requires `ACS_CONNECTION_STRING`

OTP codes replace link-based tokens for verification, password reset, and email change. OTPs are stored in Redis; length/TTL are configurable via `OTP_LENGTH` and `OTP_EXPIRE_MINUTES`.

To restrict which recipient domains the platform will send to, set `ALLOWED_EMAIL_DOMAINS` (comma-separated). An empty list allows all domains.

## Interactive Test Console
A self-contained HTML test page at `examples/test-form.html` exercises every auth flow (register, verify, login, OAuth, password reset, email change, orgs, invitations, applications, admin). Open it directly in a browser against a running API.

## Hooks and Plugins
Use `HOOK_MODULES` to register custom password rules or validation hooks.
Use `PLUGIN_MODULES` to register additional OAuth providers or MFA modules.

## Testing
```bash
pytest
```

## Metrics
If enabled, Prometheus metrics are exposed on `/metrics`.
