# Decision record — D1: keep hop 1 (RS256 bridge before Entra)

**Date:** 2026-09-17 · **Decider:** owner (Sachin) · **Status:** DECIDED — keep hop 1
**Supersedes:** the open D1 recommendation in the programme plan.

Evidence convention: `path:line` pointers are repo-relative to UserAccMgmt (this repo),
matching the plan's own convention. Nothing below is quoted; open the pointer.

CLAIM: this records D1, previously an open owner decision recommending keep hop 1 [src=docs/PLAN_identity-platform_2026-09-13.md:252-256]

## The decision

Keep hop 1. Move CloudGenie onto RS256/JWKS verification and flip UAM to RS256 now,
rather than skipping straight to Entra as the token issuer.

## Why this is safe to build against

The expensive half is already done. The UAM RS256 signing side (W1) is built and green,
behind a flag with HS256 still the default; what remains is the CloudGenie-side
verification swap and the algorithm flip.

CLAIM: W1 (UAM RS256 side) is DONE and behind a flag, HS256 still default [src=docs/PLAN_identity-platform_2026-09-13.md:119-124]
CLAIM: UAM defaults JWT_ALGORITHM to HS256 and supports RS256 as the alternative [src=app/core/config.py:19-19]

## Rationale (owner-facing)

- **Security now, not later.** Hop 1 removes the shared HS256 minting key from every
  CloudGenie customer's Azure subscription immediately, instead of leaving it exposed for
  the whole Entra long pole.

  CLAIM: hop 1 takes the minting key out of every CloudGenie customer's Azure subscription [src=docs/PLAN_identity-platform_2026-09-13.md:102-104]

- **The redeploy is not saved by skipping.** Both hop 1 and hop 2 need CloudGenie to
  verify JWKS; skipping hop 1 only defers that same work under Entra pressure. Keeping hop 1
  front-loads it and makes hop 2 a config change.

  CLAIM: after hop 1 CloudGenie is on iss/aud/kid so hop 2 is config-only [src=docs/PLAN_identity-platform_2026-09-13.md:253-254]

- **Cheap to be wrong.** The flip is config, reversible; worst case is one extra CloudGenie
  redeploy round. Token TTL is 15 minutes, so cutover windows are short.

  CLAIM: keep-hop-1 is reversible via config, cost-if-wrong is one extra redeploy round [src=docs/PLAN_identity-platform_2026-09-13.md:255-256]
  CLAIM: tokens are 15-minute so cutover windows are short [src=docs/PLAN_identity-platform_2026-09-13.md:270-271]

## What this triggers — W1-cutover (confirmation gate, cross-repo)

CLAIM: W1-cutover is outside this repo except the last step, confirmation-gated [src=docs/PLAN_identity-platform_2026-09-13.md:150-152]

1. CloudGenie replaces its shared-secret decode with JWKS verification keyed by `kid`,
   `issuer=`, `audience=`; removes `SECRET_KEY` from the JWT path (keeps it for Fernet);
   updates its bicep/env/forging test; bumps the submodule pin.

   CLAIM: cutover step 1 is CloudGenie's JWKS swap with the listed edits [src=docs/PLAN_identity-platform_2026-09-13.md:154-158]

2. Redeploy every CloudGenie customer stack — owner confirms the window per customer.

   CLAIM: cutover step 2 redeploys every customer stack with owner window confirmation [src=docs/PLAN_identity-platform_2026-09-13.md:159-159]

3. UAM flips `JWT_ALGORITHM` to RS256 on the live instance. No code.

   CLAIM: cutover step 3 is the UAM RS256 flip, config-only [src=docs/PLAN_identity-platform_2026-09-13.md:160-160]

Exit: no HS256 token is accepted anywhere; the shared secret is gone from customer infra.

CLAIM: cutover exit is no HS256 accepted and shared secret gone from customer infra [src=docs/PLAN_identity-platform_2026-09-13.md:162-162]

## Human inputs still owed after this decision

- **Per-customer maintenance windows** for the CloudGenie redeploy (step 2 above).
- **D3 — External ID tenant owner + date.** Unassigned; it gates W3 (Entra) but not hop 1.
  Hop 1 can proceed without it.

  CLAIM: D3 (tenant owner + date) is unassigned and gates W3, not hop 1 [src=docs/PLAN_identity-platform_2026-09-13.md:262-263]

## Ownership boundary

Steps 1–2 are CloudGenie work, outside both this repo and devgenie-core. Step 3 is the only
in-repo change (a live-instance config flip, no commit).
