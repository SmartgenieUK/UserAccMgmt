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
fi

az account set --subscription "${SUBSCRIPTION_ID}"

RG_NAME="$(terraform -chdir="${SCRIPT_DIR}" output -raw resource_group_name 2>/dev/null || echo "unknown")"
echo "About to destroy infrastructure in resource group: ${RG_NAME}"
read -r -p "Type DESTROY to continue: " CONFIRM

if [[ "${CONFIRM}" != "DESTROY" ]]; then
  echo "Destroy cancelled."
  exit 1
fi

echo "Running terraform destroy..."
terraform -chdir="${SCRIPT_DIR}" destroy -auto-approve
