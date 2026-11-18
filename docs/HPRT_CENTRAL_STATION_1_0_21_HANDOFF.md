# EFET / HPRT 1.0.21 — CENTRAL station

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

## Status

Production-ready code and agent packages only. This change has not been
deployed, installed, or printed.

- Branch: `cursor/hprt-central-station-549c`
- Agent version: `1.0.21`
- Source packaged by the builder: `5303a2e0d209f632d26c015802524410d54c98a7`
- Production ZIP: `app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.21.zip`
  - 1,087,982 bytes
  - SHA256 `8f2328b81c3d8a7c70bb2a8d490bb3c029e729b1a85d4ccf9aaaed00c1176514`
- Staging-shaped ZIP (not a deploy target): `app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.21-STAGING.zip`
  - 1,087,987 bytes
  - SHA256 `04aeba2d8b12582af8ad0609dd7bee89eec165abc6a9b4e74cf7881bc9874ba4`
- Previous 1.0.20 ZIPs stay in `app/static/downloads/` unchanged.
- Label size stays UNIFIED 50×70. No layout or profile change.
- Brother SR agent, Christmas labels, and ONE are unchanged.

After Rawbot review, this PR is intended for Warehouse production
(`https://sklavounoswh.up.railway.app`). Do not deploy it from this branch
without that review. Do not install it on PcDell and do not run a physical
test print until a separate OK from Christos.

## Rollback

`2b04b0e` is not the current production tip. It is
`2b04b0e78fb600dc8ecac95bb70fee732844eaca` ("feat: attest Warehouse production
releases", 30 Aug 2026), an older ancestor of this branch.

The starting tip of this work, and the commit to roll back to, is
`fbbcad55960e8095a26ac8039fabeeaa299c6a9e` ("Pin verified PostgreSQL profiles
upgrade fingerprint", 6 Sep 2026 11:35 +0300). The only successful production
web deployment on project `4cd318f3` / environment `99388a85` / service
`3e4da5fe` was created at 2026-09-06 11:25 UTC (`dd9daec8`), after that commit
and with no later commit on this branch. Rolling back to `2b04b0e` would also
remove the 1.0.20 designer.

## What changed

### Server

`POST /admin/labels/create-batch` reads `loc` from the Stock selector:

| loc | station |
| --- | --- |
| `central` | `CENTRAL` |
| `workshop` | `WORKSHOP` |
| `all`, empty, or missing | `WORKSHOP` |
| anything else | 400 |

If `station` is also sent, it must normalize to that same station. A
free-form value, or `CENTRAL` without `loc=central`, is 400. The server does
not trust the browser station text.

Role `admin` may enqueue `WORKSHOP` and `CENTRAL`. Role `workshop` may enqueue
only `WORKSHOP`; `loc=central` returns 403. Other roles stay 403.

`request_id` idempotency is unchanged. A repeat returns the original jobs and
their stored station. Label profile stays `DISTRIBUTION` / 50×70.

### Stock

The queue button and tooltip follow `loc`:

- Κεντρικό → button `🖨 Κεντρικό`, tooltip says Κεντρικό.
- Εργαστήριο → button `🖨 Εργαστήριο`.
- Όλες → button and tooltip say Εργαστήριο explicitly.
- A workshop user on Κεντρικό sees the Κεντρικό button disabled. The server
  still returns 403 if that UI is bypassed.

The Stock print-jobs list shows each job's station (Κεντρικό or Εργαστήριο).
The label-center queue does the same. Label Center itself still enqueues
`loc=workshop` only; it has no location selector.

### Agent 1.0.21

Same package. `-Station` allows only `WORKSHOP` or `CENTRAL`.

| | WORKSHOP | CENTRAL |
| --- | --- | --- |
| Install | `C:\ProgramData\Sklavounos\WarehouseHprtAgent` | `C:\ProgramData\Sklavounos\WarehouseHprtAgent-Central` |
| Scheduled task | `Sklavounos Warehouse HPRT Agent` | `Sklavounos Warehouse HPRT Agent CENTRAL` |
| Token file | `agent-token.dpapi` in the WORKSHOP folder | `agent-token.dpapi` in the CENTRAL folder |
| Setup | `SETUP.cmd` (no `-Station`) | `SETUP-CENTRAL.cmd` |

`SETUP.cmd` does not pass `-Station`, so it stays on the existing WORKSHOP
paths. Installing CENTRAL does not stop, overwrite, or read the WORKSHOP
task, folder, or token. Tokens remain `PRINT_AGENT_TOKEN_WORKSHOP` and
`PRINT_AGENT_TOKEN_CENTRAL`.

## Stock visibility note

Choosing Κεντρικό on Stock hides a product when central quantity, central
target, and pending are all zero. A product that exists only in the Workshop
can disappear from that view. That filter is unchanged. Use Όλες or
Εργαστήριο to see it. This is not a product-visibility redesign.

## Production verification checklist

No Railway deploy, no PcDell install, and no physical print are part of this
change. After a separately approved production deploy, check the following
without printing:

1. Transfers and corrections still work: Workshop → Central transfer, Central
   +/−, Workshop +/−, and a correction with a reason. Quantities on Stock
   match the movement.
2. Location Όλες: the button reads `🖨 Εργαστήριο`. Queue one label as admin.
   Response `station` is `WORKSHOP`. The Stock list shows Εργαστήριο. Do not
   let an agent claim it if that would print.
3. Location Εργαστήριο: same WORKSHOP result for both admin and workshop.
4. Location Κεντρικό as admin: button reads `🖨 Κεντρικό`. Response `station`
   is `CENTRAL`, lot code contains `-C-`, and the list shows Κεντρικό. Leave
   the job queued or cancel it. Do not install the CENTRAL agent and do not
   print.
5. Location Κεντρικό as workshop: the button is disabled. A direct
   `POST /admin/labels/create-batch` with `loc=central` returns 403 and
   creates no job.
6. `loc=freezer`, or `station=CENTRAL` without `loc=central`, returns 400.
7. Repeating the same `request_id` returns `duplicate: true` and does not
   create a second job.
8. An existing WORKSHOP agent, if left on 1.0.20, still claims only WORKSHOP.
   Do not upgrade it and do not run a test page until Christos says so.
9. Confirm the label profile in the response is still `DISTRIBUTION` and the
   payload remains the 50×70 HPRT profile.

## API expectations

```http
POST /admin/labels/create-batch
{"request_id":"...","loc":"all","label_profile":"DISTRIBUTION","items":[{"product_id":1,"copies":1}]}
```

- admin or workshop → `200`, `station: "WORKSHOP"`
- `loc: "workshop"` → `WORKSHOP`
- `loc: "central"` as admin → `200`, `station: "CENTRAL"`
- `loc: "central"` as workshop → `403`
- any other `loc` → `400`
- `station: "CENTRAL"` with missing or non-central `loc` → `400`

`GET /admin/labels/jobs` includes `station` on every row.

## Install later, only after a separate OK

Production package only, from the extracted 1.0.21 production ZIP:

- Workshop PC, idle queue: `SETUP.cmd` reuses the same folder, task, and
  same-origin DPAPI token.
- Central PC: `SETUP-CENTRAL.cmd`, then the CENTRAL token. This does not
  touch a Workshop install on another PC, and on the same PC it uses the
  other folder and task.

Do not do this as part of reviewing the PR.
