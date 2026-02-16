# Instance Profiles for Independent Deployments

Use this folder when you need multiple independently managed auth instances (for example: `app1-dev`, `app2-prod`, `app3-stage`) from one shared codebase.

## 1. Create Per-Instance Files

1. Copy `iac/instances/instance.env.example` to `iac/instances/<instance-name>.env`.
2. Set `SUBSCRIPTION_ID`, `TENANT_ID`, and optionally `ENV_NAME`.
3. Add any `TF_VAR_*` overrides specific to that instance.

Optional but recommended for team usage:

1. Copy `iac/instances/instance.backend.hcl.example` to `iac/instances/<instance-name>.backend.hcl`.
2. Set a unique backend `key` per instance.

Notes:
- `iac/instances/*.env` and `iac/instances/*.backend.hcl` are gitignored.
- Keep only `.example` files in git.

## 2. Deploy or Update One Instance

From repo root:

```bash
bash iac/deploy-instance.sh <instance-name>
```

Examples:

```bash
bash iac/deploy-instance.sh app1-dev
bash iac/deploy-instance.sh app2-prod -var='postgres_sku=GP_Standard_D2s_v3'
```

What this does:
- loads shared defaults from `iac/.env` (if present)
- loads `iac/instances/<instance-name>.env`
- initializes Terraform (and backend config if present)
- uses workspace `<instance-name>`
- applies infrastructure for only that instance workspace

## 3. Destroy One Instance

```bash
bash iac/destroy-instance.sh <instance-name>
```

The script asks for `DESTROY-<instance-name>` confirmation.

## 4. Operational Guidance

- Use one instance profile per auth deployment.
- Use unique Container App names per instance.
- Use unique DNS/base URLs per instance.
- Keep secrets in Key Vault and reference them in the runtime deployment.
- Roll out image tags independently per instance so releases do not block each other.
