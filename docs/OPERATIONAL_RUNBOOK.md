# Operational Runbook

This runbook is for day-2 operations of the Account & Identity Platform.

## 1. Service Inventory

Application:

- FastAPI auth service (`/api/v1/*`)
- Local login UI (`/login`)

Data + platform dependencies:

- PostgreSQL (`authdb`) for persistent identity data
- Redis for rate limits, lockout counters, OAuth state
- SMTP relay for verification/reset/invitation messages
- Azure Key Vault for secret storage
- Azure Monitor (Log Analytics + Application Insights)

## 2. Critical Endpoints

- Liveness: `GET /api/v1/health`
- Readiness: `GET /api/v1/ready`
- Docs: `GET /api/v1/docs`

Expected responses:

- health: `{"status":"ok"}`
- ready: `{"status":"ready"}`

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

### 3.4 Destroy Azure Infrastructure

```bash
bash iac/destroy.sh
```

## 4. Release Runbook

1. Confirm branch is green in CI.
2. Build and push image.
3. Run DB migrations (`alembic upgrade head`) against target DB.
4. Deploy new app revision.
5. Execute post-deploy checks:
- `/api/v1/health`
- `/api/v1/ready`
- `POST /api/v1/login` with known verified test account
- `POST /api/v1/refresh`
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

- Rotating `SECRET_KEY` invalidates existing JWT sessions.
- Schedule user-impacting rotations in maintenance windows.

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
