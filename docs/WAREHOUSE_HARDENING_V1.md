# Warehouse Hardening v1

Status: verified on the isolated Warehouse staging source at exact runtime
commit `b04dd6e16738d2310d374491bacf2a383aa2dfa1`. Warehouse production, SR and
Sklavounos One production remain unchanged.

## Closed in this checkpoint

- Session signing fails closed in Railway/staging/production when `SECRET_KEY`
  is missing, weak, or shorter than 32 characters.
- Session-authenticated writes reject cross-origin submissions.
- Repeated PIN failures are rate limited per username in the web process.
- Product deletion is converted to inactivation so historical stock, lot,
  missing-stock and freezer records are retained.
- Label command hooks use an argument vector with `shell=False`; source product
  data cannot be interpreted as shell syntax.
- Label and report/digest agent tokens are compared in constant time.
- Cron tokens move from URL query parameters to private request headers.
- Daily report idempotency markers are committed only after provider success;
  a failed send can be retried safely.
- PostgreSQL advisory transaction locks serialize stock deductions, transfers,
  fulfill actions, consumable stock writes and PO receipts across web workers.
- Managed environments fail startup on compatibility-DDL errors instead of
  reporting healthy with a partially migrated schema.
- Duplicate unauthenticated label routes, dead patch snippets and tracked
  bytecode are removed.
- FastAPI, Starlette, python-multipart and Jinja2 are updated to versions that
  pass the current advisory audit.
- GitHub CI now compiles, lints correctness rules and runs the complete test
  suite on Python 3.12.
- Quantity parsing and the canonical signed-stock/missing-stock rules now live
  in `app/stock_domain.py`; invalid and non-finite quantities fail closed.
- CENTRAL-to-WORKSHOP messaging now has its own router, direct role checks,
  bounded message content and serialized acknowledgement writes.
- Freezer balance writes now share a product-level transaction lock and reject
  inactive products and non-finite quantities.
- Product/category administration now has its own catalog router with unchanged
  public paths and explicit route-registry coverage.
- Freezer views and mutations now have their own router; shared quantity
  formatting keeps stock and freezer output consistent.
- A checksummed, advisory-locked migration registry now replaces guessed SQL
  ordering for new schema changes. The first migration was verified against a
  fresh production snapshot in a new isolated restore database.
- Database constraints now express the existing stock, missing, freezer,
  consumable and acknowledgement invariants. These constraints are present in
  the isolated restore and Warehouse staging only; production remains
  unchanged.
- The ORM now matches the real `NUMERIC(12,3)` production columns for product
  minimum and target quantities, and new consumable `OUT` ledger rows store a
  positive magnitude consistently with normal stock movements.

## Verified evidence

- Legacy/current dependency environment: 39 tests passed.
- Isolated upgraded dependency environment: 39 tests passed.
- `python -m compileall -q app tests`: passed.
- `ruff check app tests`: passed with correctness rules.
- `pip-audit -r requirements.txt`: no known vulnerabilities found.
- Structural follow-up environment: 90 tests passed, including direct route,
  quantity, messaging-idempotency and freezer-concurrency boundary tests.
- Fresh read-only production snapshot: PostgreSQL 17, 20 tables, 52,636 rows;
  isolated restore matched every table count and the baseline fingerprint.
- Migration rehearsal: version `20260803_001`, 11 validated constraints, four
  direct-SQL rejection checks, zero persistent test writes and a verified
  idempotent second application. Full evidence is in
  `docs/WAREHOUSE_SCHEMA_BASELINE_V1.md`.

## Executed isolated staging checkpoint

- Draft PR: `cfragoulis-design/Warehouse#6`.
- GitHub Actions PR run `30790520487`: passed compile, Ruff and all tests on
  Python 3.12.
- Exact runtime commit:
  `b04dd6e16738d2310d374491bacf2a383aa2dfa1`.
- Fresh pre-migration staging backup:
  `warehouse-staging-predeploy-20260803T063707065Z.dump`, stored only in the
  private local `data/backups/` boundary.
- Backup SHA-256:
  `f057e29a9c94bd7f9afcd3e53e41c53f24e8991ed5aa54e34a48a9a832631006`.
- Fresh restore target:
  `warehouse_staging_20260803t063707_restore_verify`.
- The restore matched the canonical baseline fingerprint, accepted migration
  `20260803_001` and produced the reviewed post-migration fingerprint. A second
  migration application was an idempotent no-op.
- Isolated staging database `warehouse_operations_staging` then accepted the
  same migration and exact commit. A second application was also a no-op.
- Staging business data after migration: 20 tables and 50,507 rows, unchanged;
  all 11 new constraints are validated.
- Railway staging deployment:
  `3ec02ca0-b829-4dc2-a325-a4b77b40a289`, status `SUCCESS`.
- The source-only runtime retained its read-only boundary: health `200`,
  summary without token `401`, wrong token `403`, correct token `200`, summary
  POST `405`, inventory `200`, and Warehouse UI login `404`.
- OpenAPI exposes only `/health`, `/api/v1/operations/summary` and
  `/api/v1/operations/inventory`. Summary aggregates stayed at 90 active, 12
  low-stock, 2 missing, 0 production today and 4 open purchase orders; the
  inventory contract returned 91 source products.
- Railway reported four error-level log records because Uvicorn writes its
  normal startup notices to stderr; every record was an `INFO` startup message
  and there was no runtime exception or failed health check.

## Executed dedicated full-UI staging checkpoint

- A second, isolated Railway service named `Warehouse Full UI Staging` was
  created in the characterization environment. It uses service ID
  `0ba277c3-8157-430b-bd76-a298c905e13b` and the non-production URL
  `https://warehouse-full-ui-staging-characterization.up.railway.app`.
- The service runs a clean archive of exact runtime commit
  `b04dd6e16738d2310d374491bacf2a383aa2dfa1`; the archive SHA-256 is
  `b2259f73749e264009edf8ff96409ea556076e6c995ecf8e06d5743329a5ad42`.
- It has a dedicated database, `warehouse_fullui_staging`, and a dedicated
  least-privilege login role with a five-connection limit. The database was
  restored from the private staging backup, then accepted migration
  `20260803_001` and its checksum-verified idempotent second application.
- Safety flags keep startup mutations, schedulers, the Operations source mode
  and both Operations read APIs disabled. No Telegram, email, print, digest or
  initial-admin provider variables were added.
- The controlled credential-rotation redeploy
  `da538b14-3437-40ac-8bf7-d3578a7b4838` completed successfully. A temporary
  password for this new isolated role appeared in a local verifier failure and
  was immediately rotated before the accepted smoke test; the old credential
  is invalid and no production credential or system was involved.
- Public boundary smoke: health and login returned `200`; anonymous Dashboard
  returned `303`; the disabled summary and inventory APIs returned `404`.
- Authenticated smoke used one random ephemeral admin, then deleted it. Login,
  logout, Dashboard, Stock, Products, Categories, Consumables, Freezer and
  Purchase Orders all returned the expected status. Cross-origin login and
  logout returned `403`, and the authenticated session was invalid after
  logout. No ephemeral user remained.
- Post-smoke read-only verification still reports 20 business tables, 50,507
  business rows, migration `20260803_001` at the exact runtime commit and all
  11 constraints validated. Railway HTTP logs contained no `5xx` response.

## Remaining transactional and human staging gate

1. Exercise the critical stock, transfer, missing, consumables and purchase
   order mutations in the dedicated full-UI staging service using disposable
   records, and reconcile their ledgers afterwards.
2. Exercise two concurrent stock deductions and two concurrent consumable takes
   against PostgreSQL; confirm the second request sees the committed balance.
3. Exercise every remaining session-authenticated POST from the real staging
   origin, including validation and authorization failures.
4. Keep Telegram/daily/weekly HTTP cron callers and all providers disabled until
   a separately approved test-recipient rehearsal changes callers atomically to
   `POST` plus `X-Digest-Token` or `X-Report-Token` headers.
5. Take another fresh backup immediately before any later production migration,
   reconfirm the migration checksum and current version, and stop on any
   mismatch. Production still requires a separate exact-commit, backup,
   rollback and deploy approval.

## Intentionally still open

- Remove startup compatibility DDL only after the versioned migration has
  passed staging and production. The baseline and registry now exist, but the
  old startup path remains temporarily for rollback compatibility.
- Add persistent, expiring worker claims for label jobs. This is a schema change
  and belongs in the first versioned migration after the baseline.
- Continue splitting `services.py` into stock and labels routers in
  behaviour-preserving checkpoints. Catalog, freezer, Workshop messaging and
  the stock-domain rules are already extracted.
- Move PIN failure counters to shared persistent storage if Warehouse is scaled
  beyond one web process/instance.
- Replace product-history `ON DELETE CASCADE` constraints after the migration
  baseline. Runtime deletion is already disabled by this checkpoint.
- Add an append-only freezer movement ledger in a later migration. Quantity and
  movement constraints are now defined, but the legacy freezer table still
  stores only the current balance.
