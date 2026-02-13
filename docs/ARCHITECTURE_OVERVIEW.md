# Architecture Overview

This document describes the runtime architecture, security model, data model, and deployment topology for the Account & Identity Platform.

## 1. System Purpose

The service provides reusable identity capabilities for internal and external applications:

- Email/password authentication
- OAuth/OIDC authentication (Google, Microsoft Entra ID)
- Multi-tenant organization membership
- Role-based authorization with scopes
- Token lifecycle management (access + rotating refresh tokens)
- Auditable security and account events

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
- Redis (rate limiting, OAuth state storage when available)
- SMTP relay (verification, reset, invitation email delivery)

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
2. Hook-based domain validation and password policy checks.
3. User row + credential row creation.
4. Personal/default organization + admin membership creation.
5. Verification token creation (hashed at rest).
6. Verification email dispatch.
7. Audit event write.

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

Access token:

- JWT (`HS256`)
- default TTL: 15 minutes
- claims include: `sub`, `email`, `role`, `org_id`, `scopes`, `exp`

Refresh token:

- default TTL: 7 days
- persisted in `refresh_tokens`
- only token hash is stored (`token_hash`)
- rotation on refresh invalidates prior token
- revocation tracked by `revoked_at`

### 5.3 Verification and Recovery

Verification token types:

- email verification
- password reset
- email change confirmation

Tokens are modeled in `verification_tokens` with expiry and one-time use semantics (`used_at`).

## 6. OAuth/OIDC Architecture

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
- `verification_tokens`
- `organizations`
- `memberships`
- `invitations`
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
- refresh token hashing at rest
- email verification gate before login
- lockout policy after repeated failures
- route + global rate limiting (Redis-backed)
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
- metrics middleware counters/histograms for request volume and latency

Azure observability services:

- Log Analytics workspace
- Application Insights linked to workspace

## 11. Configuration and Secrets

Configuration source:

- Pydantic settings (`app/core/config.py`)
- environment variables from `.env` by default

Sensitive values:

- app signing secret
- SMTP credentials
- OAuth client secrets
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
