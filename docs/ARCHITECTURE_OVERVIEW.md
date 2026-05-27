# Architecture Overview

This document describes the runtime architecture, security model, data model, and deployment topology for the Account & Identity Platform.

## 1. System Purpose

The service provides reusable identity capabilities for internal and external applications:

- Email/password authentication with OTP-based email verification
- OAuth/OIDC authentication (Google, Microsoft Entra ID)
- OAuth2 client credentials grant for machine-to-machine callers
- Per-org registered applications (client_id/secret + scope allowlist)
- Multi-tenant organization membership
- Role-based authorization with scopes
- Token lifecycle management (user access + rotating refresh tokens, client access tokens)
- Auditable security and account events
- Email abuse protection (domain allowlist, per-recipient rate limiting)

The API is exposed under `/api/v1/*`.

## 2. Architectural Style

The codebase follows layered, clean architecture boundaries:

- API layer (`app/api`): HTTP contracts, request validation, route composition
- Service layer (`app/services`): business logic, workflows, invariants
- Security layer (`app/security`): hashing, JWT, permission and auth dependencies
- Persistence layer (`app/models`, `app/db`): SQLAlchemy models, session management, DB adapters
- Cross-cutting middleware (`app/middleware`): request-id, logging, tenant context, rate limiting, metrics hooks
- Core platform concerns (`app/core`): config, exception contracts, hook/plugin registries
- Integration surface (`sdk/`, `examples/`): reusable client + reference app

This separation keeps route handlers thin and pushes policy decisions into services and security modules.

## 3. Runtime Components

Core runtime services:

- FastAPI application (`app/main.py`)
- PostgreSQL (system of record)
- Redis (rate limiting, OAuth state, OTP storage)
- Email provider — SMTP relay or Azure Communication Services (selected via `EMAIL_PROVIDER`)

Supporting Azure services (IaC in `iac/`):

- Azure Database for PostgreSQL Flexible Server
- Azure Cache for Redis
- Azure Key Vault
- Log Analytics + Application Insights
- Azure Container Apps Environment
- Optional Storage Account

## 4. High-Level Request Path

1. Request enters FastAPI.
2. Middleware executes:
- request correlation id
- structured request logging
- tenant context capture (`X-Org-Id`)
- optional metrics timing/counters
- global rate limiting
- CORS
3. Route-level dependencies run:
- token parse/verification
- membership lookup
- scope checks
4. Service layer executes business workflow.
5. SQLAlchemy persists changes to PostgreSQL.
6. Redis is used for counters and temporary state as needed.
7. Standardized error envelope is returned for domain errors.

## 5. Authentication Architecture

### 5.1 Email/Password Flow

Registration (`POST /register`) performs:

1. Email normalization and uniqueness check.
2. Domain allowlist check against `ALLOWED_EMAIL_DOMAINS` (when configured).
3. Hook-based domain validation and password policy checks.
4. User row + credential row creation.
5. Personal/default organization + admin membership creation.
6. OTP generation + storage in Redis (keyed by purpose + email, TTL `OTP_EXPIRE_MINUTES`).
7. Verification email dispatch via configured provider.
8. Audit event write.

Resend verification (`POST /resend-verification`) allows an unverified user to request a fresh OTP without re-registering. Response is constant-time and identity-agnostic to avoid account enumeration. Rate limited per IP (3/hour) and per recipient.

Login (`POST /login`) performs:

1. User + credential lookup by normalized email.
2. Verification gate (`is_verified` must be true).
3. Lockout enforcement (`lockout_until`).
4. Argon2 password verification.
5. Failed-attempt accounting + lockout progression.
6. Membership resolution (specific org or default membership).
7. Scope derivation from role.
8. Access token minting (short-lived JWT).
9. Refresh token minting (opaque token, hash stored in DB).
10. Audit event write.

### 5.2 Token Model

User access token:

- JWT (`HS256`)
- default TTL: 15 minutes
- claims include: `sub`, `email`, `role`, `org_id`, `scopes`, `exp`

Client access token (machine-to-machine):

- JWT (`HS256`)
- claims include: `sub=client:<client_id>`, `client_id`, `org_id`, `scopes`, `exp`
- issued from `POST /auth/token` via OAuth2 client credentials grant
- no refresh token — caller re-authenticates on expiry

Refresh token:

- default TTL: 7 days
- persisted in `refresh_tokens`
- only token hash is stored (`token_hash`)
- rotation on refresh invalidates prior token
- revocation tracked by `revoked_at`

### 5.3 Verification and Recovery (OTP-based)

OTP-protected flows:

- email verification
- password reset
- email change confirmation

OTP storage:

- generated as numeric codes (default 6 digits)
- persisted in Redis keyed by purpose + normalized email
- TTL from `OTP_EXPIRE_MINUTES` (default 10)
- consumed with Redis `GETDEL` to enforce one-time use
- delivered to the user via the configured email provider

The legacy link-based `verification_tokens` table is retained in the schema but no longer part of the active verification flow.

### 5.4 Email Abuse Protection

- `ALLOWED_EMAIL_DOMAINS` (optional) blocks outbound email to recipients outside the allowlist before dispatch.
- Per-recipient rate limit (default 3/hour) on register, password reset, and email change, independent of per-IP limits.
- Global rate limit is 60 req/min per IP.
- CORS requires an explicit `ALLOWED_ORIGINS` list — there is no wildcard fallback.

## 6. OAuth/OIDC Architecture

### 6.1 User Login (Authorization Code + PKCE)

Routes:

- `GET /oauth/{provider}/authorize`
- `POST /oauth/{provider}/callback`

Providers:

- Google
- Microsoft Entra ID

Flow design:

1. Authorization endpoint builds provider URL and PKCE challenge.
2. State + PKCE verifier is stored (Redis preferred, in-memory fallback).
3. Callback exchanges code for provider token.
4. User info is fetched and verified (email must be verified by provider).
5. Account linking strategy:
- existing external identity: load mapped user
- existing local user by verified email: link provider identity
- no user found: create verified user + credential placeholder + personal org
6. First membership and scopes are resolved.
7. Platform access + refresh tokens are minted.

Important operational note:

- OAuth requires provider client ids/secrets and redirect URIs in `.env`.

### 6.2 Client Credentials Grant (Machine-to-Machine)

Route: `POST /auth/token`

Flow:

1. Organization admin registers an application via `POST /orgs/{org_id}/apps`, receiving `client_id` and a one-time `client_secret`.
2. The secret is Argon2-hashed at rest (`client_secret_hash`); the plaintext is never stored.
3. Service-to-service callers exchange credentials for a client access token.
4. Granted scopes are the set intersection of `requested_scopes` and `allowed_scopes`; if no scopes are requested, all allowed scopes are granted.
5. Rate limit: 30 req/min per IP.

Admins may rotate secrets (`POST /orgs/{org_id}/apps/{app_id}/rotate-secret`) — the previous secret is immediately invalidated and a new one is returned once.

## 7. Authorization and Multi-Tenancy

### 7.1 RBAC and Scopes

Roles:

- `admin`
- `member`
- `readonly`

Scope mapping lives in `app/security/permissions.py`.

Route protection uses:

- bearer token dependency
- optional membership dependency
- `require_scopes([...])`

### 7.2 Tenant Context

Tenant context is determined by:

- `X-Org-Id` header when present
- otherwise token `org_id` claim

Membership checks ensure user has role in the selected organization before protected actions execute.

## 8. Data Architecture

Primary entities:

- `users`
- `credentials`
- `external_identities`
- `refresh_tokens`
- `verification_tokens` (legacy link-flow; OTP-based flow uses Redis)
- `organizations`
- `memberships`
- `invitations`
- `applications` — per-org OAuth2 clients (`client_id`, `client_secret_hash`, `redirect_uris`, `allowed_scopes`, `is_active`)
- `audit_events`

Design highlights:

- UUID primary keys for all major entities
- normalized email uniqueness (`users.normalized_email`)
- strict membership uniqueness (`user_id`, `org_id`)
- explicit foreign key delete behaviors
- JSONB for user `custom_fields` and audit metadata
- index coverage on high-frequency lookups (`normalized_email`, token expiry, org memberships, invitation org)

## 9. Security Controls

Implemented controls:

- Argon2 password hashing (Passlib)
- Argon2 hashing of application client secrets at rest
- refresh token hashing at rest
- email verification gate before login
- OTP one-time use (Redis `GETDEL`) + short TTL
- lockout policy after repeated failures
- route + global rate limiting (Redis-backed)
- per-email-recipient rate limiting on outbound mail
- email recipient domain allowlist (`ALLOWED_EMAIL_DOMAINS`)
- explicit `ALLOWED_ORIGINS` required for CORS (no wildcard fallback)
- structured audit logging
- standardized error contracts
- TLS-required PostgreSQL connection pattern in IaC outputs
- minimum TLS settings on managed services (IaC)

Cookie auth/CSRF model:

- cookie mode is supported by config (`USE_COOKIE_AUTH`)
- CSRF token validation is enforced for refresh in cookie mode
- default mode is bearer token usage

## 10. Observability and Operability

Application observability features:

- correlation id propagation via `X-Request-Id`
- structured request logs
- audit event log table for identity-sensitive operations
- health endpoint: `/api/v1/health`
- readiness endpoint: `/api/v1/ready` (DB + Redis check)
- machine-readable API reference: `/api/v1/help` (endpoints, scopes, roles, rate limits, password policy, enabled OAuth providers)
- metrics middleware counters/histograms for request volume and latency

Azure observability services:

- Log Analytics workspace
- Application Insights linked to workspace

## 11. Configuration and Secrets

Configuration source:

- Pydantic settings (`app/core/config.py`)
- environment variables from `.env` by default

Sensitive values:

- app signing secret (`SECRET_KEY`)
- SMTP credentials or ACS connection string (depending on `EMAIL_PROVIDER`)
- OAuth provider client secrets (Google, Microsoft)
- Application client secrets (hashed at rest, but returned once at create/rotate)
- DB and Redis credentials

Azure secret strategy:

- secrets are stored in Key Vault by Terraform
- runtime should access secrets using managed identity in production

## 12. Deployment Topology

### 12.1 Local Development

- Docker Compose starts API + Postgres + Redis
- Alembic migrates schema
- API docs exposed at `/api/v1/docs`
- Login UI exposed at `/login`

### 12.2 Azure

Terraform in `iac/` provisions:

- resource group
- PostgreSQL Flexible Server + `authdb`
- Redis cache
- Key Vault + baseline secret set
- Log Analytics + Application Insights
- Container Apps Environment
- optional storage account

Application image deployment is handled separately from IaC provisioning.

## 13. Extensibility Model

Hooks (`HOOK_MODULES`):

- custom password policy enforcement
- custom email domain restrictions
- additional request-time validation policies

Plugins (`PLUGIN_MODULES`):

- additional OAuth providers
- MFA modules

Both extension points are loaded at startup from configured module paths.

## 14. Reliability and Scaling Characteristics

Current scaling behavior:

- API is stateless except local in-memory fallbacks
- PostgreSQL and Redis are shared infrastructure backends
- refresh token and audit persistence are durable in PostgreSQL

Scale-out considerations:

- keep Redis enabled in multi-instance deployments (avoid in-memory limiter/state fallback)
- run DB migrations before app rollout
- prefer managed identity + Key Vault references over static env secrets
- use external ingress/WAF for public edge hardening

## 15. Related Docs

- User operations: `docs/USER_GUIDE.md`
- Deployment procedures: `docs/DEPLOYMENT_GUIDE.md`
