# W1-cutover runbook — big-bang (RS256/JWKS), dev-only footprint

**Date:** 2026-09-17 · **Decision inputs:** D1 = keep hop 1 (owner, 2026-09-17); rollout = big-bang,
0-1 customer stacks (owner, 2026-09-17). Supersedes the plan's per-customer-window framing for W1-cutover
while the live footprint is the dev stack only.

CLAIM: W1-cutover is the confirmation-gated CloudGenie JWKS swap + UAM RS256 flip [src=docs/PLAN_identity-platform_2026-09-13.md:150-162]
CLAIM: every CloudGenie stack points at ONE shared UAM base URL [src=CloudGenieV2/packages/shared/shared/config.py:56]

Because UAM is a single shared minting instance, the algorithm flip is global. With only the dev stack
live, big-bang (RS256-only on both sides, one window) is safe and avoids all dual-accept machinery.

## Preconditions (verify before the window)
- UAM RS256 minting is built (W1). Config supports it; the signing ring and JWKS route exist.
  CLAIM: UAM serves a JWKS endpoint at /.well-known/jwks.json [src=app/api/wellknown.py:12-15]
  CLAIM: UAM JWT_ALGORITHM defaults to HS256 with RS256 available; audience defaults to "uam" [src=app/core/config.py:19-26]
- The RS256 private key is available to the live UAM instance as the Key Vault secret `jwt-private-key-pem`
  (per the plan's W1.7 runbook entry). OWNER TO CONFIRM the secret is present.
- CloudGenie today validates HS256 with the shared secret at a single site.
  CLAIM: CloudGenie decodes the UAM JWT with the shared secret, HS256 only, in one place [src=CloudGenieV2/packages/api/api/dependencies/auth.py:59-64]

## Step 1 — CloudGenie code change (RS256-only)
1. `packages/shared/shared/config.py`: add `uam_jwks_uri`, `uam_issuer`, `uam_audience` (the file carries
   only `secret_key` for JWT today). Keep `secret_key` for Fernet.
   CLAIM: CloudGenie config has only secret_key for the JWT path [src=CloudGenieV2/packages/shared/shared/config.py:37]
2. `packages/api/api/dependencies/auth.py` `_principal_from_jwt`: replace the `jwt.decode(token,
   s.secret_key, algorithms=["HS256"])` block with RS256/JWKS verification —
   `PyJWKClient(uam_jwks_uri).get_signing_key_from_jwt(token)` then `jwt.decode(token, key,
   algorithms=["RS256"], audience=uam_audience, issuer=uam_issuer)`. Keep the claim reads (sub/role/org_id)
   and the X-Org-Id check unchanged. Update the "AUTH MODE: production ... (HS256)" log line to RS256.
3. `tests/unit/api/test_auth_dependencies.py`: the `_signed_token` helper forges with the shared secret +
   HS256 — rewrite it to mint RS256 with a throwaway test keypair and make verification use that key (inject
   the signing key / patch PyJWKClient), so no network JWKS fetch in the test.
   CLAIM: the auth test forges tokens with the shared secret via HS256 [src=CloudGenieV2/tests/unit/api/test_auth_dependencies.py:31-32]
4. `infra/bicep/cloudgenie-stack.bicep` + `main.bicep`: add `UAM_JWKS_URI`, `UAM_ISSUER`, `UAM_AUDIENCE`
   env; the `SECRET_KEY` param is no longer the JWT key (retain only for Fernet).

Config values (big-bang):
- `UAM_JWKS_URI` = `<UAM_URL>/.well-known/jwks.json`
- `UAM_ISSUER`   = UAM's `JWT_ISSUER` (defaults to UAM `PUBLIC_BASE_URL`)
- `UAM_AUDIENCE` = UAM's `JWT_AUDIENCE` (default `uam`)

## Step 2 — UAM flip (config only, no code)
On the live UAM instance set: `JWT_ALGORITHM=RS256`, `JWT_PRIVATE_KEY_PEM` (from Key Vault),
`JWT_ISSUER` (= PUBLIC_BASE_URL), `JWT_AUDIENCE=uam`. Ensure `JWT_ISSUER`/`JWT_AUDIENCE` match the three
CloudGenie values above exactly.

## Step 3 — Deploy in ONE window
1. Deploy CloudGenie (RS256 verify) — pin the image digest per the deploy rule, don't `:latest`-repoint.
2. Flip UAM to RS256 (restart/redeploy so the new env takes).
3. Smoke test: log in via UAM -> receive an RS256 token (header `alg=RS256`, `kid` present) -> call a
   CloudGenie authenticated route -> 200. Confirm an HS256 token is now rejected (401).

## Rollback (fast, reversible)
- UAM: set `JWT_ALGORITHM=HS256`, restart. Minting reverts instantly.
- CloudGenie: redeploy the prior revision (HS256 verify).
Both sides are config/revision flips; no data migration, so rollback is a few minutes.

## Post-cutover cleanup (after stable)
- Remove the shared `SECRET_KEY` from CloudGenie's JWT path entirely (retain for Fernet only until
  CloudGenie's own follow-on lands).
- The minting key is out of use the moment UAM flips (step 2); the secret's removal from CloudGenie infra
  is the tidy that closes the exposure fully.

## Owner inputs still needed
- Deploy timing: no scheduled window — deploy when convenient (owner, 2026-09-17); 15-minute token TTL keeps re-login impact minimal.
- Confirm the `jwt-private-key-pem` Key Vault secret exists and is wired to the live UAM instance.

> STEP 0 IS A HITL OWNER ACTION: see `docs/HITL_uam-rs256-key-provisioning_2026-09-17.md` (key gen + KV + RS256 env + UAM rebuild). Nothing here deploys until UAM serves JWKS.
