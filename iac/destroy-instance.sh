#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage:
  bash iac/destroy-instance.sh <instance-name> [terraform-destroy-args...]

Example:
  bash iac/destroy-instance.sh app1-dev

Behavior:
  - Loads shared defaults from iac/.env when present.
  - Loads required per-instance config from iac/instances/<instance-name>.env.
  - Uses Terraform workspace <instance-name>.
  - Optionally uses backend config at iac/instances/<instance-name>.backend.hcl.
EOF
}

INSTANCE_NAME="${1:-}"
if [[ -z "${INSTANCE_NAME}" || "${INSTANCE_NAME}" == "-h" || "${INSTANCE_NAME}" == "--help" ]]; then
  usage
  exit 1
fi
shift || true

if [[ ! "${INSTANCE_NAME}" =~ ^[a-z0-9-]{2,24}$ ]]; then
  echo "ERROR: instance-name must match ^[a-z0-9-]{2,24}$"
  exit 1
fi

COMMON_ENV_FILE="${SCRIPT_DIR}/.env"
INSTANCE_ENV_FILE="${SCRIPT_DIR}/instances/${INSTANCE_NAME}.env"
BACKEND_CONFIG_FILE="${BACKEND_CONFIG_FILE:-${SCRIPT_DIR}/instances/${INSTANCE_NAME}.backend.hcl}"

if [[ -f "${COMMON_ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${COMMON_ENV_FILE}"
  set +a
fi

if [[ ! -f "${INSTANCE_ENV_FILE}" ]]; then
  echo "ERROR: missing instance env file: ${INSTANCE_ENV_FILE}"
  echo "Create it from iac/instances/instance.env.example"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${INSTANCE_ENV_FILE}"
set +a

: "${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required in ${INSTANCE_ENV_FILE}}"
: "${TENANT_ID:?TENANT_ID is required in ${INSTANCE_ENV_FILE}}"

if [[ -z "${ENV_NAME:-}" ]]; then
  export ENV_NAME="${INSTANCE_NAME}"
fi

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

echo "Initializing Terraform for instance ${INSTANCE_NAME}..."
if [[ -f "${BACKEND_CONFIG_FILE}" ]]; then
  echo "Using backend config: ${BACKEND_CONFIG_FILE}"
  terraform -chdir="${SCRIPT_DIR}" init -upgrade -backend-config="${BACKEND_CONFIG_FILE}"
else
  echo "No backend config file found; using default backend configuration."
  terraform -chdir="${SCRIPT_DIR}" init -upgrade
fi

if ! terraform -chdir="${SCRIPT_DIR}" workspace select "${INSTANCE_NAME}" >/dev/null 2>&1; then
  echo "ERROR: workspace ${INSTANCE_NAME} does not exist."
  exit 1
fi

RG_NAME="$(terraform -chdir="${SCRIPT_DIR}" output -raw resource_group_name 2>/dev/null || echo "unknown")"
echo "About to destroy instance ${INSTANCE_NAME} in resource group: ${RG_NAME}"
read -r -p "Type DESTROY-${INSTANCE_NAME} to continue: " CONFIRM

if [[ "${CONFIRM}" != "DESTROY-${INSTANCE_NAME}" ]]; then
  echo "Destroy cancelled."
  exit 1
fi

echo "Running terraform destroy for workspace ${INSTANCE_NAME}..."
terraform -chdir="${SCRIPT_DIR}" destroy -auto-approve "$@"
