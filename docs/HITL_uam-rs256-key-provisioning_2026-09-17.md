# HITL — provision UAM's RS256 signing key (owner action, blocks W1-cutover)

**Status:** DONE 2026-10-04/05 — see "What actually happened" at the end. Originally HITL / OWNER-ONLY,
required before the W1-cutover could be deployed, followed by `docs/RUNBOOK_W1-cutover-big-bang_2026-09-17.md`.

**Why this is human-in-the-loop:** it creates production signing-key material, and the target Key Vault
trusts a different tenant than the assistant's `smartgenie.co.uk` login, so the assistant cannot reach it.

UNPINNED: on 2026-09-17 the live UAM /.well-known/jwks.json returned Not Found (image predated the RS256/JWKS code) [reason="live endpoint read on 2026-09-17, not a repo file"]
UNPINNED: the UAM Key Vault kvauthdevrmqe8 denies the assistant's token (tenant mismatch) [reason="az keyvault secret list --vault-name kvauthdevrmqe8, live Azure read, not a repo file"]

## Facts (targets)
- Container app: `ca-auth-dev-rmqe8` · resource group `rg-auth-dev-rmqe8` · subscription `SachinPersonalBasicSubscription`.
- Key Vault: `kvauthdevrmqe8` (same RG).
- Secret name UAM expects: `jwt-private-key-pem` (the plan's W1.7 naming).

UNPINNED: UAM loads the private key from JWT_PRIVATE_KEY_PEM (passphrase-less PEM, literal \n escapes tolerated) and derives the JWKS from it [reason="read in this repo at app/security/keys.py lines 19-53 on 2026-10-05; the evidence hook that checked this edit resolves paths in devgenie-core, so it cannot pin a UserAccMgmt path"]
UNPINNED: kid defaults to uam-1, audience defaults to uam, issuer defaults to PUBLIC_BASE_URL [reason="read at app/core/config.py lines 19-25 on 2026-10-05; cross-repo hook cannot pin it"]
UNPINNED: UAM requires JWT_PRIVATE_KEY_PEM when JWT_ALGORITHM=RS256 [reason="read at app/core/config.py lines 187-192 on 2026-10-05; cross-repo hook cannot pin it"]

## Steps

### 1. Generate the keypair (no passphrase)
Any of:
```
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt-private.pem
openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem
```
(PKCS8 PEM, no encryption — UAM loads it with password=None.) Keep the private PEM out of git; delete the
local copy once it is in Key Vault.

### 2. Store the private key in Key Vault
```
az keyvault secret set --subscription SachinPersonalBasicSubscription \
  --vault-name kvauthdevrmqe8 --name jwt-private-key-pem --file jwt-private.pem
```

### 3. Wire the secret onto the container app (do NOT flip RS256 here)

**Corrected 2026-10-05.** As first written this step also set `JWT_ALGORITHM=RS256`. That breaks CloudGenie
logins until CloudGenie ships its RS256 build. Here, add the key and keep `JWT_ALGORITHM` unset, so UAM keeps
signing HS256 and the JWKS is published. Flip `JWT_ALGORITHM=RS256` only in the cutover window, together with
the CloudGenie image (see "What actually happened" below).

Preferred (Key Vault reference via the app's managed identity — confirm the app HAS a user/system MI with
`get` on the vault; grant it if not):
```
az containerapp secret set --subscription SachinPersonalBasicSubscription -g rg-auth-dev-rmqe8 -n ca-auth-dev-rmqe8 \
  --secrets jwt-private-key-pem=keyvaultref:https://kvauthdevrmqe8.vault.azure.net/secrets/jwt-private-key-pem,identityref:<MI-resource-id>
az containerapp update --subscription SachinPersonalBasicSubscription -g rg-auth-dev-rmqe8 -n ca-auth-dev-rmqe8 \
  --set-env-vars JWT_PRIVATE_KEY_PEM=secretref:jwt-private-key-pem JWT_KID=uam-1 \
                 JWT_AUDIENCE=uam JWT_ISSUER=https://ca-auth-dev-rmqe8.braveforest-c553b990.uksouth.azurecontainerapps.io
```
Quick alternative (dev only, less secure — the PEM sits in app config instead of KV): set
`JWT_PRIVATE_KEY_PEM` directly as an env var value. Prefer the KV reference.

NOTE: this step ALSO requires the running UAM image to contain the RS256/JWKS code (`auth-api:v5` did
NOT). UAM must be rebuilt+redeployed from `main` (has W1 + W3/W4) as a new `auth-api` image BEFORE or
WITH this env change — see the runbook step 2. The env change alone on v5 will not serve JWKS.

### 4. Verify
```
curl -s https://ca-auth-dev-rmqe8.braveforest-c553b990.uksouth.azurecontainerapps.io/.well-known/jwks.json
```
Expect a JWKS with one key: `{"keys":[{"kty":"RSA","kid":"uam-1","use":"sig","alg":"RS256",...}]}`.
That is the green light for the CloudGenie side of the cutover (runbook step 3).

## Rotation (later, not now)
To rotate without downtime: set `JWT_PREVIOUS_PUBLIC_KEY_PEM` + `JWT_PREVIOUS_KID` to the outgoing key so
both verify during the 15-minute token overlap, then remove them after.
UNPINNED: UAM supports a previous public key + kid for rotation, both served in the JWKS [reason="read at app/security/keys.py lines 19-38 on 2026-10-05; cross-repo hook cannot pin it"]

## Checklist
- [x] Keypair generated (2048-bit, no passphrase)
- [x] Private PEM stored as jwt-private-key-pem; local copy deleted (app's own secret store, not Key Vault, see below)
- [x] App managed identity has get on the vault, or used the direct-env alternative: direct alternative used
- [x] UAM rebuilt+redeployed from main (RS256/JWKS image); RS256 flipped in the cutover window, not here
- [x] JWKS endpoint serves the uam-1 key
- [x] Hand back to the assistant to ship CloudGenie (branch w1-cutover-rs256 -> main) + flip

## What actually happened (2026-10-04/05)
- **Key storage:** the key went into the container app's own secret store (`jwt-private-key-pem`), referenced as
  `JWT_PRIVATE_KEY_PEM=secretref:jwt-private-key-pem`. Key Vault was skipped because it trusts another tenant.
  The secret is stored on one line with literal `\n` escapes, which UAM unescapes.
- **Order that worked:**
  - deploy `auth-api:v6` on HS256, with the key and JWKS published;
  - run the migrations;
  - then, in one window, CloudGenie's RS256 image first and `JWT_ALGORITHM=RS256` on UAM right after.
  - Old sessions get a 401 once and the user logs in again.
- **Bug found after the flip:** UAM's own guarded routes (`/me`, `/orgs`, admin) returned 401 for RS256 tokens,
  because `TokenPayload` forbade the `iss`/`aud` claims. Fixed in commit 7f2dde8 and shipped as `auth-api:v7`
  (revision 0000015).
- **Lessons:**
  - Run migrations inside the container as `python -m alembic upgrade head`. Plain `alembic` cannot import `app`.
  - Git Bash's openssl writes CRLF line endings, so compare public-key fingerprints only when both sides were
    hashed with the same line endings.
  - `az containerapp update` must pin the image by `@sha256:` digest.
