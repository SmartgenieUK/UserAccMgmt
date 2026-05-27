# Account Platform User Guide

This guide explains how users and internal operators interact with the Account & Identity API.

Base URL examples assume:

```text
http://localhost:8000/api/v1
```

## 1. Create an Account

Request:

```bash
curl -X POST "http://localhost:8000/api/v1/register" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "password": "StrongPass1!",
    "display_name": "Alice"
  }'
```

Expected result:
- `201 Created`
- User, credential, default organization, membership, and email verification token are saved in PostgreSQL.

## 2. Verify Email

Login is blocked until email is verified. The platform emails a one-time OTP code (default 6 digits, 10-minute TTL).

Request:

```bash
curl -X POST "http://localhost:8000/api/v1/verify-email" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "otp": "<6-digit-code-from-email>"
  }'
```

### 2.1 Resend Verification OTP

If the original code expired or was lost, request a fresh one (rate-limited to 3/hour per IP and per recipient):

```bash
curl -X POST "http://localhost:8000/api/v1/resend-verification" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com"
  }'
```

The response is the same regardless of whether the account exists, to prevent account enumeration.

## 3. Login

Request:

```bash
curl -X POST "http://localhost:8000/api/v1/login" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "password": "StrongPass1!"
  }'
```

Expected result:
- `access_token` (15 minutes)
- `refresh_token` (7 days, rotated on refresh)
- `token_type` and `expires_in`

Save `access_token` for authenticated calls:

```bash
export ACCESS_TOKEN="<access-token>"
```

## 4. Read and Update Profile

Get current user:

```bash
curl "http://localhost:8000/api/v1/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Update profile:

```bash
curl -X PATCH "http://localhost:8000/api/v1/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Alice Doe",
    "locale": "en-GB",
    "timezone": "Europe/London",
    "custom_fields": {
      "department": "Platform"
    },
    "custom_schema_version": 1
  }'
```

Deactivate own account:

```bash
curl -X DELETE "http://localhost:8000/api/v1/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## 5. Token Refresh and Logout

Refresh:

```bash
curl -X POST "http://localhost:8000/api/v1/refresh" \
  -H "Content-Type: application/json" \
  -d '{
    "refresh_token": "<refresh-token>"
  }'
```

Logout:

```bash
curl -X POST "http://localhost:8000/api/v1/logout" \
  -H "Content-Type: application/json" \
  -d '{
    "refresh_token": "<refresh-token>"
  }'
```

## 6. Password and Email Maintenance

Request password reset:

```bash
curl -X POST "http://localhost:8000/api/v1/password-reset/request" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com"
  }'
```

Confirm password reset (using the OTP code received by email):

```bash
curl -X POST "http://localhost:8000/api/v1/password-reset/confirm" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "otp": "<6-digit-code-from-email>",
    "new_password": "NewStrongPass1!"
  }'
```

Change password (authenticated):

```bash
curl -X POST "http://localhost:8000/api/v1/change-password" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "StrongPass1!",
    "new_password": "NewStrongPass1!"
  }'
```

Request email change:

```bash
curl -X POST "http://localhost:8000/api/v1/change-email/request" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "new_email": "alice.new@example.com",
    "current_password": "NewStrongPass1!"
  }'
```

Confirm email change (using the OTP code sent to the new address):

```bash
curl -X POST "http://localhost:8000/api/v1/change-email/confirm" \
  -H "Content-Type: application/json" \
  -d '{
    "new_email": "alice.new@example.com",
    "otp": "<6-digit-code-from-email>"
  }'
```

## 7. Organizations and Invitations

Create organization:

```bash
curl -X POST "http://localhost:8000/api/v1/orgs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme Ltd",
    "slug": "acme"
  }'
```

List organizations:

```bash
curl "http://localhost:8000/api/v1/orgs" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Invite member (admin role in org required):

```bash
curl -X POST "http://localhost:8000/api/v1/orgs/<org-id>/invite" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "X-Org-Id: <org-id>" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "bob@example.com",
    "role": "member"
  }'
```

Accept invitation:

```bash
curl -X POST "http://localhost:8000/api/v1/invitations/accept" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "<invitation-token>"
  }'
```

## 8. Applications (OAuth2 Clients)

Server-to-server integrations use registered applications with `client_id`/`client_secret`. Organization admins manage apps under their org.

Register an application (admin role in org required):

```bash
curl -X POST "http://localhost:8000/api/v1/orgs/<org-id>/apps" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "X-Org-Id: <org-id>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Billing Service",
    "redirect_uris": [],
    "allowed_scopes": ["apps:read", "users:read"]
  }'
```

The response includes `client_secret` exactly once — store it immediately.

List, get, update, delete:

```bash
curl "http://localhost:8000/api/v1/orgs/<org-id>/apps" \
  -H "Authorization: Bearer $ACCESS_TOKEN"

curl "http://localhost:8000/api/v1/orgs/<org-id>/apps/<app-id>" \
  -H "Authorization: Bearer $ACCESS_TOKEN"

curl -X PATCH "http://localhost:8000/api/v1/orgs/<org-id>/apps/<app-id>" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'

curl -X DELETE "http://localhost:8000/api/v1/orgs/<org-id>/apps/<app-id>" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Rotate secret (invalidates the previous secret; new one shown once):

```bash
curl -X POST "http://localhost:8000/api/v1/orgs/<org-id>/apps/<app-id>/rotate-secret" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### 8.1 Client Credentials Grant

Machine-to-machine callers exchange their `client_id`/`client_secret` for an access token:

```bash
curl -X POST "http://localhost:8000/api/v1/auth/token" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "<client-id>",
    "client_secret": "<client-secret>",
    "scopes": ["apps:read"]
  }'
```

Granted scopes are the intersection of requested scopes and the application's `allowed_scopes`. If no `scopes` are requested, all allowed scopes are granted. Tokens include `client_id`, `org_id`, and granted `scopes` claims.

## 9. Admin Operations

List users:

```bash
curl "http://localhost:8000/api/v1/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Disable or enable a user:

```bash
curl -X PATCH "http://localhost:8000/api/v1/admin/users/<user-id>/disable" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disabled": true
  }'
```

## 10. Health, Readiness, and API Discovery

Health:

```bash
curl "http://localhost:8000/api/v1/health"
```

Readiness (checks DB + Redis):

```bash
curl "http://localhost:8000/api/v1/ready"
```

Machine-readable API reference — endpoints, scopes, roles, rate limits, password policy, enabled OAuth providers:

```bash
curl "http://localhost:8000/api/v1/help"
```

## 11. Interactive Test Console

`examples/test-form.html` is a self-contained page that exercises every endpoint above with form inputs and response previews. Open it directly in a browser while the API is running. It's the fastest way to try flows end-to-end without writing client code.

## 12. Common Errors

- `401 auth_failed` / `Invalid credentials`: bad credentials or invalid token.
- `401 email_not_verified`: verify email before login.
- `401 account_locked`: too many failed logins; wait for lockout window.
- `401 invalid_otp` / `otp_expired`: OTP code was wrong or past its TTL — request a fresh one.
- `401 invalid_client`: client_id/secret wrong or application disabled.
- `403 forbidden` / `Insufficient permissions`: role or scope does not allow action.
- `403 admin_required`: endpoint requires admin role in the target organization.
- `403 email_domain_blocked`: recipient email domain is not in `ALLOWED_EMAIL_DOMAINS`.
- `409 email_exists`: email already registered.
- `422 validation_error`: request format or field value invalid.
- `429 rate_limited`: request frequency exceeded (global, per-endpoint, or per-recipient).

## 13. Security Notes for Users

- Always use HTTPS in non-local environments.
- Never share refresh tokens.
- Rotate passwords if account compromise is suspected.
- Use least privilege roles (`readonly`, `member`, `admin`) for organization users.
