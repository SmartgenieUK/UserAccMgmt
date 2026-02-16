# IaC Architecture (Azure + Terraform)

This document explains the Infrastructure-as-Code design used by this project, how resources map to application runtime needs, and how to operate it safely in dev/test and production-like environments.

## 1. Scope

Terraform code lives in `iac/` and provisions the platform foundation for the auth service:

- PostgreSQL Flexible Server + `authdb`
- Azure Cache for Redis
- Key Vault for runtime secrets
- Log Analytics + Application Insights
- Optional Container Apps Environment
- Optional Storage Account

It does not deploy app code revisions. App image rollout is intentionally separate.

## 2. File-by-File Design

- `iac/versions.tf`
  - Terraform version constraint (`>= 1.6`)
  - provider requirements
- `iac/providers.tf`
  - `azurerm` provider configuration with explicit subscription binding
  - `random` provider usage for suffix/password generation
- `iac/variables.tf`
  - all runtime knobs (subscription, tenant, region, SKUs, feature flags, tags)
- `iac/main.tf`
  - full resource graph and dependency wiring
- `iac/outputs.tf`
  - values required by app deploy/operations
- `iac/deploy.sh`
  - non-interactive init/apply flow
- `iac/destroy.sh`
  - guarded destroy flow
- `iac/deploy-instance.sh`
  - per-instance init/workspace/apply flow
- `iac/destroy-instance.sh`
  - per-instance workspace destroy flow
- `iac/env.example`
  - bootstrap for `TF_VAR_*` and required deployment IDs
- `iac/instances/`
  - per-instance env and backend templates (`*.example`)

## 3. Resource Graph

Provisioning order (logical):

1. Naming/password randomness (`random_string`, `random_password`)
2. Resource Group
3. Observability base (Log Analytics)
4. App Insights linked to workspace
5. PostgreSQL server + DB + server security configs + firewall rules
6. Redis Cache
7. Key Vault + deployer access policy
8. Key Vault secrets populated from Postgres/Redis outputs
9. Optional Container Apps Environment
10. Optional Storage Account

Dependency highlights:

- Key Vault secrets depend on access policy and on data-store resources.
- Container Apps environment depends on Log Analytics.
- Postgres security config resources depend on Postgres server ID.

## 4. Naming and Tag Strategy

All names use a deterministic prefix plus a random suffix to avoid global-name collisions:

- resource group: `rg-auth-<env>-<suffix>`
- postgres: `psql-auth-<env>-<suffix>`
- redis: `redis-auth-<env>-<suffix>`
- key vault: trimmed to Azure naming limits

Tags merge a standard baseline with user-provided tags:

- `environment`
- `managed_by=terraform`
- `workload=auth-platform`
- custom `tags` input

## 5. Security-by-Default Controls

Configured now:

- Postgres admin password generated via `random_password`.
- Postgres secure transport required (`require_secure_transport=on`).
- Postgres minimum TLS protocol set (`ssl_min_protocol_version=TLSv1.2`).
- Redis minimum TLS 1.2, non-SSL port disabled.
- Secrets persisted to Key Vault, not hardcoded in source.
- Postgres connection string generated with `sslmode=require`.

Current networking defaults are dev-friendly:

- public access enabled for Postgres/Redis/Key Vault
- optional Azure services firewall rule on Postgres

Production hardening path:

1. Disable public access and move to private networking.
2. Replace Key Vault access policy model with RBAC.
3. Use managed identity for app secret retrieval.
4. Restrict Postgres firewall to explicit build/admin IPs.

## 6. Variables and Operational Knobs

Core required inputs:

- `subscription_id`
- `tenant_id`
- `env_name`

Common tuning inputs:

- `location` (default `uksouth`)
- `postgres_sku` (default `B_Standard_B1ms`)
- `postgres_storage_mb` (default `32768`)
- `redis_sku` (default `Basic`)
- `redis_capacity` (default `0`, C0)
- `enable_container_apps_env` (default `true`)
- `enable_storage_account` (default `true`)

Network/security toggles:

- `postgres_public_network_access_enabled`
- `allow_azure_services`
- `postgres_allowed_ips`

## 7. Outputs and App Runtime Mapping

Terraform outputs are directly consumable by app deployment automation:

- `postgres_server_fqdn` + `postgres_database_name`
- `redis_hostname`
- `key_vault_name`
- `application_insights_connection_string`
- `log_analytics_workspace_id`
- `container_apps_environment_id`

Key Vault secrets map to app config:

- `POSTGRES-CONNECTION-STRING` -> `DATABASE_URL`
- `REDIS-HOSTNAME` + `REDIS-PRIMARY-KEY` -> Redis runtime config

## 8. Deployment Workflow

Recommended flow:

1. Populate `iac/.env` from `iac/env.example`.
2. Run `iac/deploy.sh`.
3. Capture outputs for app deployment automation.
4. Deploy app image/revision separately.
5. Run application migrations from app pipeline/startup job.

Destroy flow:

- `iac/destroy.sh` requires explicit confirmation and prints target resource group.

Multi-instance flow (recommended when running multiple app instances):

1. Create `iac/instances/<instance>.env` from `iac/instances/instance.env.example`.
2. Optional: create `iac/instances/<instance>.backend.hcl` from `iac/instances/instance.backend.hcl.example`.
3. Run `bash iac/deploy-instance.sh <instance>`.
4. The script selects or creates workspace `<instance>` and applies only that workspace.
5. Destroy with `bash iac/destroy-instance.sh <instance>`.

## 9. State and Drift Management

State:

- local state is acceptable for local testing
- remote backend is recommended for teams (Azure Storage backend with locking strategy)
- per-instance backend keys + per-instance workspaces are recommended for independent instance lifecycle operations

Drift handling:

- use `terraform plan` before each apply
- import pre-existing resources when needed
- avoid ad-hoc portal changes on managed resources

Known implemented drift guard:

- Postgres `zone` changes are ignored in lifecycle to avoid unsupported update paths for this SKU/setup.

## 10. Cost and Environment Lifecycle Guidance

For short-lived testing:

- keep default burstable Postgres + Basic Redis
- destroy environment when idle
- disable optional features if not needed (`enable_storage_account=false`, `enable_container_apps_env=false`)

For persistent environments:

- move to remote state backend
- define workload-specific tags for chargeback
- add budget alerts at subscription/resource-group scope

## 11. Troubleshooting Checklist

Common failures and checks:

1. Provider registration issues:
- register required namespaces (example: `Microsoft.App` for Container Apps resources)

2. Authorization issues:
- verify Azure role assignments and Entra directory permissions

3. Postgres config apply errors:
- verify accepted configuration value format for provider/API version

4. Slow Redis creation:
- Redis provisioning can take significantly longer than stateless resources; this is expected in some regions/SKUs

5. Key Vault access denied:
- validate deployer identity and access policy or RBAC assignment

## 12. References

- Detailed IaC operator guide: `iac/README.md`
- Platform deployment process: `docs/DEPLOYMENT_GUIDE.md`
- System architecture: `docs/ARCHITECTURE_OVERVIEW.md`
