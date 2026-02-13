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

Login is blocked until email is verified.

Request:

```bash
curl -X POST "http://localhost:8000/api/v1/verify-email" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "<verification-token-from-email>"
  }'
```

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

Confirm password reset:

```bash
curl -X POST "http://localhost:8000/api/v1/password-reset/confirm" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "<reset-token-from-email>",
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

Confirm email change:

```bash
curl -X POST "http://localhost:8000/api/v1/change-email/confirm" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "<email-change-token-from-email>"
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

## 8. Admin Operations

List users:

```bash
curl "http://localhost:8000/api/v1/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Disable user:

```bash
curl -X PATCH "http://localhost:8000/api/v1/admin/users/<user-id>/disable" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "disable": true
  }'
```

## 9. Health and Readiness

Health:

```bash
curl "http://localhost:8000/api/v1/health"
```

Readiness:

```bash
curl "http://localhost:8000/api/v1/ready"
```

## 10. Common Errors

- `401 auth_failed` / `Invalid credentials`: bad credentials or invalid token.
- `401 email_not_verified`: verify email before login.
- `401 account_locked`: too many failed logins; wait for lockout window.
- `403 forbidden` / `Insufficient permissions`: role or scope does not allow action.
- `409 email_exists`: email already registered.
- `422 validation_error`: request format or field value invalid.
- `429 rate_limited`: request frequency exceeded.

## 11. Security Notes for Users

- Always use HTTPS in non-local environments.
- Never share refresh tokens.
- Rotate passwords if account compromise is suspected.
- Use least privilege roles (`readonly`, `member`, `admin`) for organization users.
