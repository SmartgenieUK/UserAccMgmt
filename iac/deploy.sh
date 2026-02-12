#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
fi

: "${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required. Copy iac/env.example to iac/.env and set it.}"
: "${TENANT_ID:?TENANT_ID is required. Copy iac/env.example to iac/.env and set it.}"
: "${ENV_NAME:?ENV_NAME is required. Copy iac/env.example to iac/.env and set it.}"

export TF_VAR_subscription_id="${SUBSCRIPTION_ID}"
export TF_VAR_tenant_id="${TENANT_ID}"
export TF_VAR_env_name="${ENV_NAME}"

if [[ -n "${LOCATION:-}" ]]; then
  export TF_VAR_location="${LOCATION}"
fi

if ! az account show >/dev/null 2>&1; then
  az login --tenant "${TENANT_ID}" >/dev/null
else
  CURRENT_TENANT="$(az account show --query tenantId -o tsv)"
  if [[ "${CURRENT_TENANT}" != "${TENANT_ID}" ]]; then
    az login --tenant "${TENANT_ID}" >/dev/null
  fi
fi

az account set --subscription "${SUBSCRIPTION_ID}"

echo "Running terraform init..."
terraform -chdir="${SCRIPT_DIR}" init -upgrade

echo "Running terraform apply..."
terraform -chdir="${SCRIPT_DIR}" apply -auto-approve

echo
echo "Deployment complete. Key outputs:"
echo "Resource Group: $(terraform -chdir="${SCRIPT_DIR}" output -raw resource_group_name)"
echo "Postgres FQDN:  $(terraform -chdir="${SCRIPT_DIR}" output -raw postgres_server_fqdn)"
echo "Database Name:  $(terraform -chdir="${SCRIPT_DIR}" output -raw postgres_database_name)"
echo "Key Vault:      $(terraform -chdir="${SCRIPT_DIR}" output -raw key_vault_name)"
echo "Redis Hostname: $(terraform -chdir="${SCRIPT_DIR}" output -raw redis_hostname)"
echo "App Insights:   $(terraform -chdir="${SCRIPT_DIR}" output -raw application_insights_connection_string)"
echo "Log Analytics:  $(terraform -chdir="${SCRIPT_DIR}" output -raw log_analytics_workspace_id)"

CA_ENV_ID="$(terraform -chdir="${SCRIPT_DIR}" output -raw container_apps_environment_id 2>/dev/null || true)"
if [[ -n "${CA_ENV_ID}" && "${CA_ENV_ID}" != "null" ]]; then
  echo "Container Apps: ${CA_ENV_ID}"
else
  echo "Container Apps: disabled"
fi
