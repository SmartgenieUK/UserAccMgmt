# Operational Runbook

This runbook is for day-2 operations of the Account & Identity Platform.

## 1. Service Inventory

Application:

- FastAPI auth service (`/api/v1/*`)
- Local login UI (`/login`)

Data + platform dependencies:

- PostgreSQL (`authdb`) for persistent identity data
- Redis for rate limits, lockout counters, OAuth state, OTP codes
- Email provider — SMTP relay or Azure Communication Services (`EMAIL_PROVIDER`) for verification/reset/invitation messages
- Azure Key Vault for secret storage
- Azure Monitor (Log Analytics + Application Insights)

## 2. Critical Endpoints

- Liveness: `GET /api/v1/health`
- Readiness: `GET /api/v1/ready`
- Docs: `GET /api/v1/docs`
- API reference: `GET /api/v1/help`

Expected responses:

- health: `{"status":"ok"}`
- ready: `{"status":"ready"}`
- help: JSON with endpoints, scopes, roles, rate limits, password policy, enabled OAuth providers

## 3. Standard Operating Procedures

### 3.1 Start Locally (Docker)

```bash
docker compose up -d --build
docker compose exec -T -e PYTHONPATH=/app api alembic upgrade head
```

Smoke test:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-login.ps1
```

### 3.2 Stop Locally

```bash
docker compose down
```

### 3.3 Deploy/Update Azure Infrastructure

```bash
bash iac/deploy.sh
```

For independently managed instances:

```bash
bash iac/deploy-instance.sh <instance-name>
```

### 3.4 Destroy Azure Infrastructure

```bash
bash iac/destroy.sh
```

For independently managed instances:

```bash
bash iac/destroy-instance.sh <instance-name>
```

## 4. Release Runbook

1. Confirm branch is green in CI.
2. Build and push image.
3. Run DB migrations (`alembic upgrade head`) against target DB.
4. Deploy new app revision.
5. Execute post-deploy checks:
- `/api/v1/health`
- `/api/v1/ready`
- `/api/v1/help` (verifies config surfaces correctly)
- `POST /api/v1/login` with known verified test account
- `POST /api/v1/refresh`
- `POST /api/v1/auth/token` with a known test application (client credentials grant)
6. Monitor error rate, p95 latency, and 401/429 spikes for 15 minutes.

Rollback:

1. Roll app image to previous known-good tag.
2. If schema changed, ensure backward compatibility before rollback.
3. Re-run readiness and login smoke checks.

## 5. Incident Response

Severity model:

- Sev1: Total auth outage, widespread login failures, data integrity risk
- Sev2: Partial degradation, elevated auth errors, OAuth provider outage impact
- Sev3: Minor defect, low user impact

First 10 minutes:

1. Check `/health` and `/ready`.
2. Check app logs and request IDs for failing paths.
3. Confirm PostgreSQL connectivity and Redis ping.
4. Identify blast radius:
- all users vs single tenant/org
- password flow vs OAuth only
- read endpoints vs write endpoints
5. Communicate incident status + ETA for next update.

## 6. Failure Playbooks

### 6.1 Login Failures (401 spike)

Check:

1. Email verification status (`is_verified`) for affected accounts.
2. Lockout fields (`failed_login_attempts`, `lockout_until`) in `credentials`.
3. JWT secret mismatch across revisions.
4. Token expiration/time skew issues.

Mitigation:

1. Roll back revision if regression introduced.
2. Clear unintended lockouts only after confirmation.
3. If provider-specific, disable affected OAuth button temporarily in UI/docs.

### 6.2 PostgreSQL Unavailable

Symptoms:

- `/ready` fails
- DB timeout/connection errors

Actions:

1. Validate server health in Azure portal/CLI.
2. Confirm network/firewall settings match client source.
3. Validate connection string secret in Key Vault.
4. Fail over to previous healthy app revision only if issue is app-induced.

### 6.3 Redis Unavailable

Symptoms:

- rate-limiting failures or degraded behavior
- readiness may fail if Redis required

Actions:

1. Validate Redis service health.
2. Check TLS/auth configuration.
3. For emergency continuity, run with `REDIS_REQUIRED=false` only in non-production.

### 6.4 OAuth Failing

Symptoms:

- `/oauth/{provider}/authorize` or callback errors

Checks:

1. `GOOGLE_*` / `MICROSOFT_*` env values are set.
2. Redirect URIs match exactly:
- `http://localhost:8000/login/oauth/google/callback`
- `http://localhost:8000/login/oauth/microsoft/callback`
3. Provider client secret expiry/revocation.
4. Entra app permissions/consent state.

## 7. Data Operations

### 7.1 Verify Account Persistence

```bash
docker compose exec -T db psql -U authuser -d authdb -c "SELECT email,is_verified,created_at FROM users ORDER BY created_at DESC LIMIT 20;"
```

### 7.2 Backup Strategy (Azure PostgreSQL)

Baseline:

- Managed backups are enabled by server policy.
- Validate retention and restore windows periodically.

Operational checks:

1. Verify latest restorable point.
2. Test restore into non-prod target monthly.
3. Document restore duration and validation checklist.

### 7.3 Restore Drill (Recommended)

1. Restore server to test instance/time point.
2. Validate schema (`alembic current`) and key tables.
3. Run login smoke test on restored environment.
4. Record RTO/RPO outcomes.

## 8. Secret Rotation Runbook

Rotate:

- app `SECRET_KEY`
- SMTP credentials
- OAuth client secrets
- DB/Redis credentials (according to platform policies)

Process:

1. Write new secret values to Key Vault.
2. Update app secret references/env.
3. Restart app revision.
4. Validate login + refresh token flow.
5. Revoke old credentials after validation.

Important:

- Under `HS256`, rotating `SECRET_KEY` invalidates existing JWT sessions.
- Under `RS256`, `SECRET_KEY` only backs CSRF (cookie mode); the signing key is `JWT_PRIVATE_KEY_PEM`.
- Schedule user-impacting rotations in maintenance windows.

Signing-key rotation (`RS256`):

1. Generate a new RSA-2048 private key; store it in Key Vault as `jwt-private-key-pem`.
2. Move the current public key to `JWT_PREVIOUS_PUBLIC_KEY_PEM` and its kid to `JWT_PREVIOUS_KID`.
3. Set `JWT_PRIVATE_KEY_PEM` to the new key and bump `JWT_KID` (e.g. `uam-2`).
4. Restart the revision. `/.well-known/jwks.json` now lists both kids; tokens minted before the restart
   verify until they expire (15 minutes).
5. After the access-token TTL has elapsed, clear the `JWT_PREVIOUS_*` values and restart once more.

## 9. Security Operations

Daily checks:

1. Monitor failed login trend and lockout spikes.
2. Monitor repeated 429 by source.
3. Review admin actions (`/admin/users/*`) via audit events.
4. Verify no secrets were committed to git.

Weekly checks:

1. Dependency vulnerability scan (`pip-audit`).
2. Review Terraform drift (`terraform plan`).
3. Review key role assignments in Azure.

## 10. Capacity and Cost Controls

For short-lived environments:

1. Destroy when idle (`bash iac/destroy.sh`).
2. Keep default low-cost SKUs unless load requires scaling.
3. Disable optional resources if unused:
- `enable_container_apps_env=false`
- `enable_storage_account=false`

For sustained environments:

1. Add budget alerts at subscription/resource-group level.
2. Track DB CPU/storage growth and Redis memory pressure.
3. Right-size SKUs based on 2-4 weeks of telemetry.

## 11. Maintenance Windows

Before window:

1. Announce impact and expected duration.
2. Confirm backups and rollback image tag.
3. Freeze non-essential merges.

During window:

1. Apply infra/app changes.
2. Run migration and smoke checks.
3. Monitor logs and alerts.

After window:

1. Confirm service stability.
2. Publish completion status.
3. Capture lessons learned.

## 12. On-Call Quick Commands

Health/readiness:

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/ready
```

Container status:

```bash
docker compose ps
docker compose logs api --tail=200
```

OAuth setup helper:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-oauth.ps1 -RestartContainers
```

Login smoke:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-login.ps1
```

## 13. Documentation Map

- System architecture: `docs/ARCHITECTURE_OVERVIEW.md`
- IaC architecture: `docs/IAC_ARCHITECTURE.md`
- Deployment guide: `docs/DEPLOYMENT_GUIDE.md`
- User/API guide: `docs/USER_GUIDE.md`

## 14. Entra Custom-Claims-Provider (Token-Issuance) Monitoring

### 14.1 Overview and Tier-0 Criticality

- **Endpoint:** `POST /api/v1/entra/token-issuance`
- **Role:** Synchronously invoked by Microsoft Entra ID as a custom authentication extension (custom-claims-provider) during user sign-in to enrich the minted token with entitlement claims (`tier`, `org_id`, `account_id`, `scope`).
- **Callout budget:** Entra enforces a strict ~2 s execution budget (including retries).
- **Fail-closed behaviour:** If this endpoint fails or times out, Entra will not mint the token. An outage or latency degradation on this route blocks product sign-in platform-wide (Sev1).
- **Performance invariant:** The hot path executes zero outbound network calls (public signing keys are pinned in config; runtime JWKS fetches are prohibited). Execution time is dominated by a single indexed database lookup (`find_or_create_user` and `EntitlementService.resolve`), typically completing in under 50–100 ms.

### 14.2 Health Probes and Availability Checks

- **Synthetic probe:** Configure an Azure Application Insights standard Web Test or external synthetic monitor targeting `POST /api/v1/entra/token-issuance`.
- **Cheap auth-reject probe:** Issue a lightweight `POST` without a bearer token (or with a dummy bearer token).
  - Response expected: `401 Unauthorized` (`{"detail":"Missing bearer token","code":"entra_no_bearer"}`).
  - Assert `HTTP 401` as the passing health condition. This validates ingress routing, TLS termination, middleware chain, and FastAPI app availability without triggering database writes or mutating state.
- **Exempt from rate limits and CSRF:**
  - The endpoint is registered in `_RATE_LIMIT_EXEMPT` and uses stateless bearer authentication (CSRF exempt).
  - High-frequency synthetic probes will not be throttled or blocked by the Redis rate limiter.

### 14.3 Latency Monitoring and Thresholds

- **Telemetry:** Monitor request duration in Application Insights for `POST /api/v1/entra/token-issuance`.
- **Alert thresholds:**
  - **Warning (p95 > 1.0 s):** Alert on-call. Leaves a 1.0 s safety buffer before Entra's 2 s cutoff. Indicates database contention or connection pool pressure.
  - **Critical (p95 > 1.5 s or max > 1.8 s):** Immediate risk of Entra timeout errors and widespread login failure.
- **Latency baseline:** Because no external network hops exist, any latency increase is attributable to PostgreSQL query performance, pool exhaustion, or container CPU throttling.

### 14.4 Key Alert — Entra Sign-In Log Error 1003005

- **Alert code:** `1003005 CustomExtensionTimedOut`
- **Meaning:** Microsoft Entra reached its ~2 s callout limit without receiving a response from UAM and aborted token issuance.
- **Impact:** Every single occurrence of this code is a user-visible sign-in failure.
- **Telemetry surface:** Streamed from Entra ID Diagnostic Settings into Azure Log Analytics (`SignInLogs` table).
- **Log Analytics alert query (KQL):**
  ```kusto
  SigninLogs
  | where ResultType == "1003005"
  | project TimeGenerated, UserPrincipalName, AppDisplayName, IPAddress, ResultType, ResultDescription
  ```
- **Alert rule configuration:** Fire a Sev1 alert when `count > 0` over a 5-minute rolling window.

### 14.5 Hosting Guard (No Scale to Zero)

- **Minimum replicas:** The Container App instance must maintain `min-replicas >= 1` at all times (`az containerapp update --min-replicas 1 ...`). Never scale to zero.
- **Cold-start risk:** A cold start (container boot, Python runtime initialisation, dependency loading, database pool connection) requires several seconds, which immediately exhausts Entra's 2 s callout budget.
- **Production naming caveat:** The current live deployment carries `dev` in its resource naming (for example, workspace/profile `app1-dev` and container app names), but it serves as the live production sign-in dependency. It must never be idled, paused, or treated as disposable development infrastructure.

### 14.6 First-Response Triage Checklist

When responding to claims endpoint degradation or `1003005` alerts:

1. **Confirm endpoint availability:**
   - Probe the endpoint directly from CLI:
     ```bash
     curl -i -X POST https://<uam-host>/api/v1/entra/token-issuance
     ```
   - Confirm `401 Unauthorized` is returned promptly. Check `/api/v1/health` and `/api/v1/ready`.
2. **Check recent deployments:**
   - Check recent container app revisions or configuration updates. If a deployment introduced a regression, roll back immediately to the last known-good revision (`az containerapp revision activate ...`).
3. **Check database health and latency:**
   - Inspect PostgreSQL Flexible Server metrics (CPU utilisation, connection count, query duration).
   - Check application logs for database pool timeouts or lock contention.
4. **Check pinned Entra signing key configuration:**
   - Verify `ENTRA_SIGNING_PUBLIC_KEY_PEM`, `ENTRA_ISSUER`, and `ENTRA_EXTENSION_APP_ID` in Key Vault and environment variables.
   - If Entra rotated its authentication-events signing keys in the tenant, the pinned key will reject valid callouts with `401 entra_token_invalid`. Update `ENTRA_SIGNING_PUBLIC_KEY_PEM` from tenant metadata and restart the revision.
5. **Verify fail-closed expectation:**
   - The claims provider must fail closed. Under no circumstances should a token without an entitlement `tier` be issued to unblock sign-in.
   - A `5xx` error is the correct and expected system response to database faults or internal errors (preventing unauthorised access); minting a token with missing or incorrect claims is not acceptable.

