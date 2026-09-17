# HITL — provision UAM's RS256 signing key (owner action, blocks W1-cutover)

**Status:** HITL / OWNER-ONLY — required before the W1-cutover can be deployed. Do this first, then follow
`docs/RUNBOOK_W1-cutover-big-bang_2026-09-17.md`. Owner to action 2026-09-18 (first thing).

**Why this is human-in-the-loop:** it creates production signing-key material, and the target Key Vault
trusts a different tenant than the assistant's `smartgenie.co.uk` login, so the assistant cannot reach it.

CLAIM: the live UAM /.well-known/jwks.json returns Not Found today (image predates the RS256/JWKS code) [src=https://ca-auth-dev-rmqe8.braveforest-c553b990.uksouth.azurecontainerapps.io/.well-known/jwks.json]
CLAIM: the UAM Key Vault kvauthdevrmqe8 denies the assistant's token (tenant mismatch) [src=az keyvault secret list --vault-name kvauthdevrmqe8]

## Facts (targets)
- Container app: `ca-auth-dev-rmqe8` · resource group `rg-auth-dev-rmqe8` · subscription `SachinPersonalBasicSubscription`.
- Key Vault: `kvauthdevrmqe8` (same RG).
- Secret name UAM expects: `jwt-private-key-pem` (the plan's W1.7 naming).

CLAIM: UAM loads the private key from JWT_PRIVATE_KEY_PEM (passphrase-less PEM, literal \n escapes tolerated) and derives the JWKS from it [src=app/security/keys.py:19-24,41-53]
CLAIM: UAM requires JWT_PRIVATE_KEY_PEM when JWT_ALGORITHM=RS256; kid defaults to uam-1; audience defaults to uam; issuer defaults to PUBLIC_BASE_URL [src=app/core/config.py:19-26,184-190]

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

### 3. Wire the secret + RS256 env onto the container app
Preferred (Key Vault reference via the app's managed identity — confirm the app HAS a user/system MI with
`get` on the vault; grant it if not):
```
az containerapp secret set --subscription SachinPersonalBasicSubscription -g rg-auth-dev-rmqe8 -n ca-auth-dev-rmqe8 \
  --secrets jwt-private-key-pem=keyvaultref:https://kvauthdevrmqe8.vault.azure.net/secrets/jwt-private-key-pem,identityref:<MI-resource-id>
az containerapp update --subscription SachinPersonalBasicSubscription -g rg-auth-dev-rmqe8 -n ca-auth-dev-rmqe8 \
  --set-env-vars JWT_ALGORITHM=RS256 JWT_PRIVATE_KEY_PEM=secretref:jwt-private-key-pem JWT_KID=uam-1 \
                 JWT_AUDIENCE=uam JWT_ISSUER=https://ca-auth-dev-rmqe8.braveforest-c553b990.uksouth.azurecontainerapps.io
```
Quick alternative (dev only, less secure — the PEM sits in app config instead of KV): set
`JWT_PRIVATE_KEY_PEM` directly as an env var value. Prefer the KV reference.

NOTE: this step ALSO requires the running UAM image to contain the RS256/JWKS code (current `auth-api:v5`
does NOT). UAM must be rebuilt+redeployed from `main` (has W1 + W3/W4) as a new `auth-api` image BEFORE or
WITH this env change — see the runbook step 2. The env flip alone on v5 will not serve JWKS.

### 4. Verify
```
curl -s https://ca-auth-dev-rmqe8.braveforest-c553b990.uksouth.azurecontainerapps.io/.well-known/jwks.json
```
Expect a JWKS with one key: `{"keys":[{"kty":"RSA","kid":"uam-1","use":"sig","alg":"RS256",...}]}`.
That is the green light for the CloudGenie side of the cutover (runbook step 3).

## Rotation (later, not now)
To rotate without downtime: set `JWT_PREVIOUS_PUBLIC_KEY_PEM` + `JWT_PREVIOUS_KID` to the outgoing key so
both verify during the 15-minute token overlap, then remove them after.
CLAIM: UAM supports a previous public key + kid for rotation, both served in the JWKS [src=app/security/keys.py:23-24,32-38]

## Checklist
- [ ] Keypair generated (2048-bit, no passphrase)
- [ ] Private PEM stored in kvauthdevrmqe8 as jwt-private-key-pem; local copy deleted
- [ ] App managed identity has get on the vault (or used the direct-env alternative)
- [ ] UAM rebuilt+redeployed from main (RS256/JWKS image) with the RS256 env set
- [ ] JWKS endpoint serves the uam-1 key
- [ ] Hand back to the assistant to ship CloudGenie (branch w1-cutover-rs256 -> main) + flip
