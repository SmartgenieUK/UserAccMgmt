# Deployment Guide

This guide covers deployment of the Account & Identity Platform for:

1. Local/dev with Docker Compose
2. Azure infrastructure provisioning with Terraform
3. Azure Container Apps runtime deployment

For infrastructure architecture detail (resource graph, security model, variables, outputs), also see:

- `docs/IAC_ARCHITECTURE.md`
- `iac/README.md`

## 1. Prerequisites

Install:

- Docker + Docker Compose
- Python 3.11 (optional for local non-container execution)
- Azure CLI (`az`) for Azure deployments
- Terraform `>= 1.6` for Azure IaC (`iac/`)

## 2. Local Deployment (Docker Compose)

### 2.1 Configure environment

From repo root:

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

- `SECRET_KEY` (32+ chars)
- `DATABASE_URL`
- `REDIS_URL`
- SMTP values (`SMTP_HOST`, `SMTP_PORT`, etc.)

For local Compose defaults, the included `.env.example` already points to:

- PostgreSQL at `db:5432`
- Redis at `redis:6379`

### 2.2 Start services

```bash
docker-compose up --build -d
```

### 2.3 Run database migrations

```bash
docker-compose exec api alembic upgrade head
```

### 2.4 Verify health

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/ready
```

Expected response:

- `{"status":"ok"}` for health
- `{"status":"ready"}` for readiness

### 2.5 Access API docs

Open:

```text
http://localhost:8000/api/v1/docs
```

## 3. Azure Infrastructure Deployment (Terraform)

Infrastructure code lives under `iac/` and provisions:

- Resource Group
- PostgreSQL Flexible Server + `authdb`
- Redis Cache
- Key Vault (stores connection/auth secrets)
- Log Analytics Workspace
- Application Insights
- Optional Container Apps Environment
- Optional Storage Account

### 3.1 Configure IaC environment

```bash
cp iac/env.example iac/.env
```

Set:

- `SUBSCRIPTION_ID`
- `TENANT_ID`
- `ENV_NAME`

Optional `TF_VAR_*` settings in `iac/.env` let you tune SKUs/capacity and feature flags.

### 3.2 Deploy infrastructure

```bash
bash iac/deploy.sh
```

The script will:

1. Validate Azure login/tenant
2. Set subscription
3. Run `terraform init`
4. Run `terraform apply -auto-approve`
5. Print key outputs

### 3.3 Destroy infrastructure

```bash
bash iac/destroy.sh
```

The script includes a safety confirmation (`DESTROY`) and prints the resource group before deletion.

## 4. Build and Push API Image

Build image from repo root:

```bash
docker build -t <registry>/<repo>/useraccmgmt:<tag> .
```

Push:

```bash
docker push <registry>/<repo>/useraccmgmt:<tag>
```

Use your registry of choice (for Azure, Azure Container Registry is typical).

## 5. Deploy to Azure Container Apps

If `enable_container_apps_env=true`, `iac` creates the environment used here.

### 5.1 Create Container App (first deploy)

```bash
az containerapp create \
  --name ca-auth-api \
  --resource-group <resource-group-name> \
  --environment <container-apps-env-name> \
  --image <registry>/<repo>/useraccmgmt:<tag> \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 3
```

### 5.2 Configure runtime secrets and env vars

Set non-secret env vars directly:

```bash
az containerapp update \
  --name ca-auth-api \
  --resource-group <resource-group-name> \
  --set-env-vars \
    APP_NAME=\"Account & Identity Platform\" \
    API_V1_PREFIX=\"/api/v1\" \
    ACCESS_TOKEN_EXPIRE_MINUTES=\"15\" \
    REFRESH_TOKEN_EXPIRE_DAYS=\"7\" \
    REDIS_REQUIRED=\"true\"
```

Set secret-backed vars (recommended):

- `DATABASE_URL` from Key Vault secret `POSTGRES-CONNECTION-STRING`
- Redis host/key from `REDIS-HOSTNAME` and `REDIS-PRIMARY-KEY`
- `SECRET_KEY` from a dedicated Key Vault secret

Recommended production pattern:

1. Enable managed identity on Container App.
2. Grant identity Key Vault secret read permission.
3. Use Key Vault secret references in Container App secrets.

## 6. Post-Deployment Validation Checklist

Run these checks after deployment:

1. `GET /api/v1/health` returns `ok`
2. `GET /api/v1/ready` returns `ready`
3. Registration flow persists user rows in PostgreSQL
4. Login blocked before email verification
5. Login works after email verification
6. Refresh token rotation works
7. Redis rate-limits are active
8. Logs/traces visible in Application Insights

## 7. Rollback Strategy

Use immutable image tags and rollback by redeploying a known-good tag:

```bash
az containerapp update \
  --name ca-auth-api \
  --resource-group <resource-group-name> \
  --image <registry>/<repo>/useraccmgmt:<previous-good-tag>
```

For schema changes, use Alembic migration strategy with backward-compatible deployments where possible.

## 8. Production Hardening Recommendations

Before production go-live:

1. Move PostgreSQL and Key Vault behind private endpoints.
2. Disable PostgreSQL public access and lock down firewall rules.
3. Use Key Vault RBAC with least privilege.
4. Rotate admin and app secrets regularly.
5. Enable WAF/API gateway in front of public ingress.
6. Configure backup/restore and DR procedures for PostgreSQL.
7. Add alerting on auth anomalies (lockouts, failed logins, spike in 401/429).
