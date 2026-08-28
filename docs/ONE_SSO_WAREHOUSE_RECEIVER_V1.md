# One SSO → Warehouse receiver v1

This is a default-off, authorization-code style bridge. The browser never
receives a One identity assertion, Google token, Warehouse client credential or
Warehouse session value. It posts only a short-lived opaque code to Warehouse.

## Browser contract

`POST /auth/one/callback`

- exact `Origin` header equal to `ONE_SSO_ORIGIN`;
- exact `Content-Type: application/x-www-form-urlencoded`;
- bounded body and declared `Content-Length`;
- exactly one `version=1` and one `code` field;
- code alphabet and length exactly `[A-Za-z0-9_-]{32,256}`;
- no query token, identity fields, JWT, `next` path or duplicate fields.

Before any exchange, the public callback applies a proxy-independent,
process-wide admission gate: at most 30 enabled callback attempts per 60
seconds and at most four concurrent One exchanges. A limited request receives
`429`, `Retry-After` and `Cache-Control: no-store`; repeated limited attempts
produce at most one denial audit per 60 seconds. This is defense in depth in
addition to Railway edge controls, not an identity or authorization decision.

Warehouse redeems the code server-to-server with a bounded TLS request that
does not follow redirects:

```http
POST {ONE_SSO_EXCHANGE_URL}
Authorization: Bearer {ONE_SSO_CLIENT_SECRET}
X-One-SSO-Client: {ONE_SSO_CLIENT_ID}
Content-Type: application/json

{"version":1,"app_code":"warehouse","code":"..."}
```

The response must contain exactly `version`, `subject`, `employee_id`, `email`,
`display_name`, `app_code`, `assurance_level`, `permissions`, `scopes`,
`issued_at` and `expires_at`. Warehouse requires:

- `version == 1` and `app_code == "warehouse"`;
- `assurance_level >= ONE_SSO_REQUIRED_ASSURANCE_LEVEL` (default `2`);
- `external.warehouse.launch`;
- a current, short-lived assertion;
- canonical UUID `subject` and `employee_id` values plus an exact
  pre-provisioned email (when pinned) mapping;
- an exact One location/department scope;
- an active mapping and active local user whose role still matches.

No account or role is created during login. A successful exchange creates a
normal Secure, HttpOnly, SameSite Warehouse session. Existing Warehouse
role/action/location checks remain authoritative. Redeemed codes are fenced by
a keyed digest; codes and response bodies are never stored or audited.

## Configuration

The feature remains unavailable unless all of these are present:

```text
ONE_SSO_ENABLED=true
ONE_SSO_ORIGIN=https://one.example
ONE_SSO_EXCHANGE_URL=https://one.example/api/v1/external-access/exchange
ONE_SSO_CLIENT_ID=warehouse-staging
ONE_SSO_CLIENT_SECRET=<dedicated random value, at least 32 characters>
ONE_SSO_REQUIRED_ASSURANCE_LEVEL=2
ONE_SSO_TIMEOUT_SECONDS=3
ONE_SSO_MAX_ASSERTION_LIFETIME_SECONDS=120
ONE_SSO_SESSION_TTL_SECONDS=28800
```

The origin and exchange URL must be exact HTTPS values on the same origin. The
client secret must be unique to Warehouse and must not be shared with SR or a
browser.

## Mapping provisioning

Migration `20260828_002` adds the inactive-by-policy mapping boundary. Provision
only after that migration succeeds. The script is read-only unless `--apply` is
explicitly supplied and requires exact database, local username and employee ID
confirmations.

Plan example:

```powershell
python scripts/provision_one_sso_mapping.py `
  --expected-database warehouse_fullui_staging `
  --confirm-database warehouse_fullui_staging `
  --local-username workshop-one `
  --local-role workshop `
  --local-location-code WORKSHOP `
  --one-subject <stable-one-subject> `
  --one-employee-id <stable-one-employee-id> `
  --one-location-id <one-location-uuid> `
  --one-department-id <one-department-uuid> `
  --expected-email employee@example.com
```

Run the same command with `--apply`, `--confirm-local-username workshop-one`
and `--confirm-one-employee-id <stable-one-employee-id>` only after reviewing
the plan. The local account must already exist, be active and have the exact
role. Admin mapping additionally requires `--allow-admin` and a reviewed global
scope; it is never inferred.

## Rollback and local login

Set `ONE_SSO_ENABLED=false` to close the callback without deleting mappings.
Set a mapping or local user inactive to invalidate its existing One-backed
session on the next request. One-backed local sessions also expire after eight
hours by default and can never be configured beyond sixteen hours. Local PIN
login remains available as a controlled
compatibility/break-glass path and is labelled as such whenever SSO is enabled.

No Production migration, mapping or activation is part of this candidate.
