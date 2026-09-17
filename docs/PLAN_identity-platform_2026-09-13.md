# Identity-platform programme — UserAccMgmt plan (2026-09-13)

**What this is.** The build plan for turning UserAccMgmt ("UAM", this repo) into SmartGenie's account /
org / entitlement / billing layer, with Microsoft Entra External ID minting the tokens underneath and
devgenie-core (DevGenie's MCP backend) verifying them as an OAuth resource server. It turns the fitness
audit's surgical change groups into sequenced, gated work packages.

**Inputs (read these, do not trust copies).**
- Owner decision, 2026-08-19: `devgenie-core/docs/co-arch/BRIEF_ga-auth-portal_2026-08-19.md` — UAM is the
  company-wide auth product; Entra External ID is the OAuth engine under the covers; the HS256→RS256 break
  is accepted; retire UAM's local credential half; never run two auth systems.
- Fitness audit, 2026-09-13: `devgenie-core/docs/co-arch/AUDIT_useraccmgmt-fitness_2026-09-13.md` —
  verdict reuse-and-extend; change groups (a)–(e) in §5; sequencing in §7. This plan follows its order.
- CloudGenie's ratified direction, 2026-06-14: `CloudGenieV2/docs/architecture/uam-signing-model.md` —
  RS256 + JWKS with per-consumer `aud`; consumers cut over before UAM withdraws HS256.
- devgenie-core's side of the contract: `devgenie-core/docs/execplans/xp-cr133-oauth-resource-server.md`
  (CR-133, execution-ready, not built).

Evidence convention: `path:line` pointers into this repo at HEAD `d2ba388` (tree clean). Foreign repos are
prefixed. Nothing below is quoted; open the pointer.

---

## 1. What changed since the audit — three unknowns resolved

**U1. SmartConsent is not a UAM consumer.** The brief says UAM is "in production with CloudGenie and
SmartConsent". It is not. SmartConsent (`SmartgenieUK/SmartConsent`, HEAD `87ad11c`, 2026-04-15) mints and
verifies its own lead tokens with its own secret and never calls UAM.
- Evidence: `SmartConsent/api/services/auth.py:11-30` (own `create_lead_token` / `verify_lead_token`,
  HS256 via python-jose with `settings.api_secret_key`).
- Search (re-runnable on a clone): `grep -rniE 'useraccmgmt|ca-auth-|/api/v1/auth|X-Org-Id|jwks' --include='*.py' --include='*.ts' --include='*.tsx' --include='*.md' --include='*.bicep' --include='*.env*' .` → 0 hits in code; the only `azurecontainerapps.io` hits are SmartConsent's own API URL. No `.gitmodules`.
- Consequence: **the consumer migration is CloudGenie only.** SmartConsent joins later as a *new* consumer
  and starts at the end state (Entra-issued RS256, JWKS verify), per the signing-model doc's rule for
  net-new consumers. Its own lead tokens are out of scope — they are not user identity.

**U2. The Entra custom-claims-provider contract** (Microsoft Learn, `custom-claims-provider-reference`,
`custom-extension-overview`, `custom-extension-troubleshoot`; all `updated_at 2026-06-15`):
- Request: `type` = `microsoft.graph.authenticationEvent.tokenIssuanceStart`; `data.@odata.type` =
  `microsoft.graph.onTokenIssuanceStartCalloutData`; `data.authenticationContext` carries `correlationId`,
  `clientServicePrincipal.appId` (the calling app — maps to product), `resourceServicePrincipal.appId`,
  and `user.{id, mail, displayName, userPrincipalName, userType}`.
- Response: `data.@odata.type` = `microsoft.graph.onTokenIssuanceStartResponseData`; exactly one action
  with `@odata.type` = `microsoft.graph.tokenIssuanceStart.provideClaimsForToken` and a `claims` object.
- **Claim values are string or string-array only.** No boolean, no JSON object. Total claims ≤ **3 KB**.
  So `tier`, `org_id`, `account_id` are strings and `scope` is a string array; no nested entitlement blob.
- Claims reach the token only through a **claims mapping policy** on the app (case-sensitive `ID` →
  `JwtClaimType`). The policy is tenant config (WP1), not code.
- **Timeout 2 seconds, then retries; if retries fail, authentication fails and no token is minted**
  (OIDC apps: user redirected back with an error). Entra is fail-closed by construction. Consequence:
  **UAM is a Tier-0 dependency of every sign-in for every product on the tenant.** The claims endpoint must
  answer from one indexed query, never call out, and must be monitored as sign-in availability.
- Inbound auth: Entra calls with a bearer token. Validate `azp` (v2) or `appid` (v1) =
  `99045fe1-7639-4a75-9d4a-577b6ca3810f` (the authentication-events service), `aud` = the extension's own
  app-registration id, `iss` = `https://{domainName}.ciamlogin.com/{tenantId}/v2.0` for an External tenant.
- Token-issuance-start is supported on External (customer) tenants.

**U3. The test suite is red, and for a different reason than predicted.** Run on 2026-09-13 in a
scratch venv from `requirements.txt` + `requirements-dev.txt`: **1 failed, 2 passed**.
- `tests/test_auth.py::test_register_login_flow` fails at the first INSERT with
  `sqlite3.ProgrammingError: Error binding parameter 1: type 'UUID' is not supported` — before it reaches
  the stale `verification_tokens` assertion the audit flagged.
- Cause: `app/db/types.py:4` maps the sqlite variant to `String(36)`, which does not coerce `uuid.UUID`
  objects on bind. Any test that writes a row fails on sqlite; the two passing tests never write.
- Also resolved: `cryptography` (50.0.1) is installed transitively by `authlib`, so RS256 works in the
  image today; still pin it explicitly (W1.6).

---

## 2. Work packages

Gate tags follow devgenie-core's convention: `none` = safe unattended; `precondition` = needs a fact or an
external artefact first; `confirmation` = needs the owner. Sizes S/M/L are code effort in this repo.

### W0 — Hardening slice (gate: none · size S · no dependencies)

Goal: a test base that can carry the rest. Every later package adds tests; they must be able to pass.

| # | Change | File | Acceptance |
|---|---|---|---|
| W0.1 | Replace the sqlite UUID variant with SQLAlchemy 2.0's generic `Uuid(as_uuid=True)` (native UUID on Postgres, CHAR on sqlite, coerces both ways) | `app/db/types.py:4` | `test_register_login_flow` gets past the INSERT |
| W0.2 | Rewrite the stale auth test for the OTP path: register → read the OTP from the (fake) Redis/email hook → `/verify-email` with `email`+`otp` → login | `tests/test_auth.py:11-38`; the OTP is written at `app/services/auth_service.py:69-70` | test passes; `verification_tokens` is not asserted |
| W0.3 | Delete the dead link-token helpers | `app/services/auth_service.py:231-264` | rg for `_create_verification_token` and `_consume_token` under `app/` → 0 hits |
| W0.4 | Fix multi-org membership resolution: replace `scalar_one_or_none()` on the unfiltered query with an explicit default-org rule (earliest membership by `created_at`) | `app/services/auth_service.py:275-276`; same pattern at `app/services/oauth_service.py:167-168` and `:177-179` | new test: user in two orgs → login without `org_id` returns the earliest; refresh does not 500 |
| W0.5 | RSA test keypair fixture (generated per session, never checked in) + stop tests forging tokens with the shared secret | `tests/conftest.py:29` | fixture exists; used by W1 tests |
| W0.6 | Replace the deprecated `event_loop` override with `asyncio_default_fixture_loop_scope` in `pytest.ini`; use `ASGITransport` | `tests/conftest.py:40-44`, `:66`; `pytest.ini` | no pytest-asyncio / httpx deprecation warnings |
| W0.7 | Add `test.db` to `.gitignore` | `.gitignore` | `git status` clean after pytest |

Exit: `pytest` green, 3 → ~6 tests, no warnings.

**W0 status (2026-09-14): DONE, uncommitted.** `pytest` = 4 passed, 0 warnings, five consecutive runs.
Two things differed from the table above once the code was exercised: (1) `Uuid` alone was not enough —
the services pass ids as strings (audit events, `session.get`) which asyncpg coerced silently, so
`app/db/types.py` now carries a `UUIDType` decorator that coerces `str → uuid.UUID` on bind; (2) sqlite
returns naive datetimes for `DateTime(timezone=True)`, which broke the refresh path's expiry comparison, so
a `TZDateTime` decorator re-attaches UTC on read and every model's timestamp column now uses `TZ_DATETIME`
(schema unchanged on PostgreSQL). W0.6 was met by making the engine fixture function-scoped instead of
adding a loop-scope option. The multi-org test pins the second membership's `created_at` explicitly because
sqlite's `CURRENT_TIMESTAMP` is second-resolution and tied.

### W1 — RS256 + JWKS bridge, UAM side (gate: none · size S–M · depends W0)

Audit group (c), hop 1. Keep it even though Entra supersedes it: it takes the minting key out of every
CloudGenie customer's Azure subscription now, and it moves CloudGenie onto `iss`/`aud`/`kid` verification so
hop 2 is a config change. See decision D1.

| # | Change | File |
|---|---|---|
| W1.1 | `app/security/keys.py`: load the private key PEM from config; expose the public JWK set; support two `kid`s (active + previous) | new |
| W1.2 | Config: `JWT_ALGORITHM` default `RS256`; add `JWT_PRIVATE_KEY_PEM`, `JWT_PREVIOUS_PUBLIC_KEY_PEM` (rotation), `JWT_KID`, `JWT_ISSUER` (= `PUBLIC_BASE_URL`), `JWT_AUDIENCES` (product → aud string). `SECRET_KEY` stays for CSRF only | `app/core/config.py:16-17` |
| W1.3 | Mint with `iss`, `aud`, header `kid`, private key. Both user and client tokens | `app/security/jwt.py:29`, `:50` |
| W1.4 | Verify with the public key, `algorithms=["RS256"]` **only**, `audience=` UAM's own aud, `issuer=`. Never list HS256 next to RS256 | `app/security/jwt.py:55` |
| W1.5 | `GET /.well-known/jwks.json` and a minimal `/.well-known/openid-configuration` (`issuer`, `jwks_uri`) mounted at root beside `web_router` | new `app/api/wellknown.py`; `app/main.py:57` |
| W1.6 | Pin `cryptography` | `requirements.txt` |
| W1.7 | Key Vault secret for the PEM (lean bridge; record as bridge, not end state); rotation runbook with two `kid`s | `iac/main.tf:145-158`; `docs/OPERATIONAL_RUNBOOK.md:224`; `docs/ARCHITECTURE_OVERVIEW.md:102-115` |
| W1.8 | Tests: mint→verify round-trip with the W0.5 fixture; wrong `aud` rejected; HS256 token rejected; JWKS serves both kids; `alg=none` rejected | `tests/test_jwt_rs256.py` |

Exit: UAM can issue RS256 behind a flag while still issuing HS256 by default (the flip is W1-cutover below).

**W1 status (2026-09-14): DONE (commit bff1de7).** Deviations from the table: `JWT_ALGORITHM` keeps
`HS256` as the default (the flip is the cutover's last step, by config); hop 1 uses a **single**
`JWT_AUDIENCE` rather than a per-product map, because login has no product context today — per-consumer
`aud` arrives naturally in hop 2, where each product is its own Entra app registration. `iac/main.tf`
defines no secrets at all (they are set by hand per the deployment guide), so W1.7 is the runbook entry
naming the Key Vault secret `jwt-private-key-pem`, not a Terraform change.

### W2 — Entitlement model + API (gate: none · size M · depends W0; parallel with W1)

Audit group (a). Two tables; entitlement is derived, never stored per user.

| # | Change | File |
|---|---|---|
| W2.1 | `plans(id, product, code lite/pro/enterprise, features JSONB, limits JSONB, is_active)` — seeded by migration | new `app/models/plan.py` |
| W2.2 | `subscriptions(id, org_id, product, plan_id, status trialing/active/past_due/cancelled, seats, current_period_end, source stripe/manual, stripe_customer_id, stripe_subscription_id, timestamps)`; unique `(org_id, product)` | new `app/models/subscription.py` |
| W2.3 | Seat = membership flagged per product: `memberships.products JSONB default []` (column first; a seat table only if per-seat audit is later required) | `app/models/membership.py:12-31` |
| W2.4 | Migration `…_000003_plans_subscriptions.py`; register models; org relationship | `alembic/versions/`, `app/models/__init__.py`, `app/models/organization.py:20-22` |
| W2.5 | `EntitlementService.resolve(org_id, product) → {tier, scope[], seats, seats_used, expires_at, org_id, account_id}`; no row ⇒ **Lite**; `past_due`/`cancelled` ⇒ Lite (fail to free, never to paid) | new `app/services/entitlement_service.py` |
| W2.6 | Routes: `GET /entitlements/me?product=`; `GET /orgs/{org_id}/entitlements` (admin); `PUT /orgs/{org_id}/entitlements/{product}` (admin, `source=manual`) | new `app/api/v1/entitlements.py`; mount in `app/api/v1/api.py:13-20` |
| W2.7 | Scopes `entitlements:read` (all roles), `billing:write` (admin) | `app/security/permissions.py:5-21`; help text `app/api/v1/health.py:254-271` |
| W2.8 | Audit events `subscription_changed`, `seat_assigned`, `entitlement_overridden` | callers of `app/services/audit_service.py:15-36` |
| W2.9 | Tests: default Lite; manual override; seat cap; past_due ⇒ Lite; scope enforcement | `tests/test_entitlements.py` |

Exit: devgenie-core's CR-133 slice can gate against a real resolver using W1 test tokens — no Entra needed.

**W2 status (2026-09-14): DONE.** As tabled, plus seat assign/unassign routes (`POST`/`DELETE
/orgs/{id}/entitlements/{product}/seats…`) so the seat cap is exercisable; `status`/`source` surfaced on the
entitlement; statuses/sources are plain strings validated in code (no new PostgreSQL enum types). Plans are
seeded from `DEFAULT_PLANS` in `app/models/plan.py` by both the migration and the tests; the seed carries
empty `features`/`limits` — the per-tier scope and seat defaults are a product decision still to be entered.

### W1-cutover — CloudGenie verifies RS256, then UAM flips (gate: confirmation · CloudGenie S code, M ops)

Outside this repo except the last step. Tokens live 15 minutes (`app/core/config.py:18`), so a per-customer
maintenance-window cutover beats a verify-both window.
1. CloudGenie: replace the shared-secret decode at `CloudGenieV2/packages/api/api/dependencies/auth.py:60`
   with JWKS verification keyed by `kid`, `issuer=` and `audience=` from config; remove `SECRET_KEY` from
   the JWT path (keep for Fernet until CloudGenie's own follow-on lands); update
   `CloudGenieV2/infra/bicep/cloudgenie-stack.bicep:321`, `staging-env.md:25`/`:79`, and the forging test
   `CloudGenieV2/tests/unit/api/test_auth_dependencies.py:31`; bump the submodule pin from `58c3408`.
2. Redeploy every CloudGenie customer stack (owner confirms the window per customer).
3. UAM: flip `JWT_ALGORITHM` to RS256 on the live instance. No code.

Exit: no HS256 token is accepted anywhere; the shared secret is gone from customer infrastructure.

### W3 — Entra External ID + claims provider (gate: precondition — tenant exists · size S–M code, M–L tenant work · depends W2)

Audit group (b). The long pole; start the tenant work (brief WP1) as soon as W0 is done.

**STATUS 2026-09-14 — W3 core DONE (commit `a2a3584`), tenant-independent slice.** W3.1–W3.4 and the
JIT half of W3.5 built test-first; `tests/test_entra_claims.py` 8 tests green, full suite 34 passed.
**Deviation from the original plan:** the endpoint is issuer-agnostic — `ENTRA_ISSUER` selects a
*workforce* tenant (`login.microsoftonline.com/{tenant}/v2.0`) as readily as an External ID tenant
(`ciamlogin.com`). Microsoft supports token-issuance-start custom claims providers on *both* tenant
types (Learn, `custom-extension-tokenissuancestart-configuration`, 2026-09-14), so W3 is **no longer
blocked on standing up a separate External ID tenant** — it can run against the existing
`smartgenie.co.uk` workforce tenant (`e11f2537-…`, Entra ID P1). Remaining before live exercise:
confirm the `99045fe1-…`-authorised extension app registration exists in that tenant, tenant-side
claims-mapping-policy config (WP1), and W3.8 monitoring. **Deferred:** W3.5's oauth_service migration +
random-password `Credential` drop, and W3.6 portal provider — both touch live login, so they get their
own change + review. **Rule 7 review OWED** (codex BLOCKED 4%, agy unrecorded at build time).

| # | Change | File |
|---|---|---|
| W3.1 | Config: `ENTRA_TENANT_ID`, `ENTRA_TENANT_DOMAIN`, `ENTRA_ISSUER`, `ENTRA_JWKS_URL`, `ENTRA_CLIENT_ID/SECRET` (UAM's portal RP), `ENTRA_EXTENSION_APP_ID` (aud for inbound calls), `ENTRA_PRODUCT_APP_IDS` (client `appId` → product) | `app/core/config.py:53-60` |
| W3.2 | `app/security/entra_jwt.py`: RS256 verify via `PyJWKClient`; enforce `iss`, `aud`; for the claims endpoint additionally enforce `azp`/`appid` = `99045fe1-7639-4a75-9d4a-577b6ca3810f` | new |
| W3.3 | `POST /entra/token-issuance`: validate bearer (W3.2) → map `clientServicePrincipal.appId` → product → JIT-link or create user + personal org from `user.id`/`user.mail` → `EntitlementService.resolve` → respond with the exact `@odata.type` shapes in §1 U2; claims as strings / string arrays only; ≤ 3 KB. One indexed query path; **no outbound calls**; p95 well under 1 s (Entra's budget is 2 s). Exempt from the per-IP rate limiter and CSRF | new `app/api/v1/entra.py`; `app/middleware/rate_limit.py` |
| W3.4 | `ExternalProvider.ENTRA_EXTERNAL`; unique index on `(provider, provider_user_id)` (none today: `alembic/versions/20260212_000001_initial.py:71-80`) | `app/models/enums.py:17-19`; `app/models/external_identity.py:12-30`; migration |
| W3.5 | Extract "find by provider sub → else verified email → else create user + personal org" into `app/services/identity_link_service.py`; drop the random-password `Credential` at `app/services/oauth_service.py:136` | `app/services/oauth_service.py:103-146` |
| W3.6 | `EntraExternalProvider` (authority `https://<domain>.ciamlogin.com/<tenant-id>/oauth2/v2.0/…`) registered via the existing `PluginRegistry` — the portal's sign-in during the transition | `app/services/oauth_providers.py:91-104`; `app/api/v1/oauth.py:28-29`, `:52-53` |
| W3.7 | Tests: signed fixture JWT → correct claims response; wrong `azp` → 401; unknown subject ⇒ JIT create; DB down ⇒ 5xx (so Entra's fail-closed policy engages, never a token with no `tier`) | `tests/test_entra_claims.py` |
| W3.8 | Availability: health probe on the claims path; alert on Entra sign-in-log error `1003005 CustomExtensionTimedOut`; App Insights latency on the route | `docs/OPERATIONAL_RUNBOOK.md` |

Tenant work (outside this repo, brief WP1): External ID tenant; app registrations per product (DevGenie MCP
client, CloudGenie, portal); custom-authentication-extension registration with `identifierUris` in the
`api://{fqdn}/{appid}` form and admin consent for `CustomAuthenticationExtensions.Receive.Payload`; the
claims mapping policy mapping `tier`, `org_id`, `account_id`, `scope`; social IdPs (Google/Microsoft)
configured **in Entra**, not in UAM.

Exit: a DevGenie sign-in against the tenant yields an Entra RS256 token carrying `tier`; devgenie-core
CR-133 verifies it against Entra's JWKS with only config changes.

### W4 — Stripe billing (gate: precondition — Stripe products exist · size M · depends W2; parallel with W3)

**STATUS 2026-09-14 — W4 DONE (commit `11378c6`), code + tests; live wiring parked.** Built by agy
(gemini-3.1-pro-high) against a pinned brief, verified by Claude (Rule 7). 9 billing tests, full suite 44
passed. Checkout/portal/GET-billing routes, root `POST /webhooks/stripe` (signature-verified, idempotent via
`stripe_events`), `BillingService` as sole `source=stripe` writer with tier ALWAYS from the server-held
`STRIPE_PRICE_MAP` (never a client field), `organizations.stripe_customer_id`, migration `20260914_000005`.
Deferred: live Stripe products + keys/config (deploy edge) and a DB-level guard for the concurrent-duplicate
-webhook race (currently get-then-insert -> Stripe-retry-safe).

Audit group (d), unchanged: `POST /orgs/{id}/billing/checkout-session`, `POST …/portal-session` (Stripe
Customer Portal instead of our own screens), `GET …/billing`; root-mounted `POST /webhooks/stripe` with
signature check + idempotent event ids; `BillingService` is the only writer of `source=stripe` rows;
`organizations.stripe_customer_id`; audit events; replayed-fixture tests. Entitlement wiring is automatic
through W2.5. Note SmartConsent has its own Stripe integration (`SmartConsent/api/services/stripe_service.py`);
leave it — it bills leads, not orgs.

### W5 — Retire the local auth half + user migration (gate: confirmation · size S–M code, M coordination · depends W3 live and CloudGenie login re-pointed)

Audit group (e), unchanged in content, **last** and in step with CloudGenie's UI, which calls UAM
`/register`, `/verify-email`, `/resend-verification`, `/login`, `/oauth/*`, `/refresh`, `/me` today
(`CloudGenieV2/packages/platform/src/components/LoginPage.tsx:31-119`, `OAuthCallback.tsx:41-53`,
`hooks/useApi.ts:35`). Delete: password/OTP/login/reset routes + service + schemas; credential and
verification-token models; password hooks and limits; OTP/reset emails; user refresh tokens + `/refresh`
+ `/logout`; cookie/CSRF mode (then `SECRET_KEY` goes); Google/Microsoft RP federation; the hard-coded
login/callback pages. Keep: M2M client-credentials + `applications` until the separate M2M decision.
Hop 2 for CloudGenie = issuer/JWKS/aud config only. Argon2 password users cannot be imported into Entra —
they re-register or sign in socially and are linked by verified email; that needs comms and a hard
time-box, because the overlap is the "two auth systems" state the guardrail forbids.

---

## 3. Sequencing

```
W0 hardening ──┬─▶ W1 RS256 bridge ──▶ W1-cutover (CloudGenie redeploy, UAM flip)
               │
               ├─▶ W2 entitlement ──┬─▶ W3 Entra + claims ──▶ W5 retire + migrate
               │                    └─▶ W4 Stripe
               │
tenant work (WP1) starts after W0, runs alongside W1/W2, must be done before W3.3 can be exercised live
```

- W1 and W2 touch disjoint files and can be built in parallel by two builders with one integrator.
- devgenie-core CR-133 can be built any time after W2 lands (it gates on test tokens + a test JWKS). It
  goes live only after W3.
- W4 is independent of W3; do it whenever Stripe products are set up.
- W5 is a single coordinated cutover; nothing in it is unattended.

## 4. Decisions for the owner

**D1 — keep hop 1 (W1 + W1-cutover) or skip straight to Entra? DECIDED 2026-09-17: KEEP HOP 1 (owner) — see `docs/DECISION_D1_keep-hop1-rs256_2026-09-17.md`.** With SmartConsent out, hop 1 costs one
CloudGenie per-customer redeploy and buys: the minting key out of customer infra now, and CloudGenie already
on `iss`/`aud`/`kid` so hop 2 is config. Skipping it saves that redeploy but leaves the shared-secret
exposure open for the whole Entra long pole. **Recommend: keep hop 1.** Reversible: yes (config).
Cost if wrong: one extra CloudGenie redeploy round.

**D2 — default-org rule for multi-org users (W0.4).** Earliest membership vs a `users.default_org_id`
column. **Recommend earliest membership** now (no schema change); add the column only if a product needs
"switch default org". Reversible: yes.

**D3 — who owns the External ID tenant (subscription, admins, cost centre).** Outside this repo; it gates
W3. Not a code decision — needs an owner and a date.

## 5. Risks (updated)

1. **UAM is Tier-0 for sign-in once W3 is live** — Entra does not mint if the claims call fails after
   retries. Mitigation: W3.3's no-outbound-call rule, W3.8 monitoring, and a hosting plan that never
   scales to zero for the live instance (`ca-auth-dev-…` is named `dev` and is the production dependency).
2. **CloudGenie cutover is per customer**, twice (hop 1 redeploy, hop 2 config). Mitigation: D1 accepts
   this knowingly; tokens are 15-minute so windows are short.
3. **Claim shape is string-only and ≤ 3 KB** — no nested entitlement object. devgenie-core's CR-133 must
   read `tier` as a string claim and treat absence as Lite/deny. Feed this back into the CR-133 execplan
   before it is built.
4. **User migration is human-visible** (W5). Comms, not code.
5. **Test base was red** — W0 fixes it before anything is extended.

## 6. Not in this plan

Portal UI (brief WP3, a separate front-end, doubles as the landing site); MCP-of-the-auth-product (brief,
additive, later); moving M2M client-credentials to Entra app registrations; SmartConsent's own lead auth.
