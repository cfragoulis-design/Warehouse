# Warehouse Hardening v1

Status: local verified candidate with a successful isolated production-clone
migration rehearsal. No staging or production deployment has been performed.

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
  the candidate and isolated clone only; production remains unchanged.
- The ORM now matches the real `NUMERIC(12,3)` production columns for product
  minimum and target quantities, and new consumable `OUT` ledger rows store a
  positive magnitude consistently with normal stock movements.

## Verified evidence

- Legacy/current dependency environment: 39 tests passed.
- Isolated upgraded dependency environment: 39 tests passed.
- `python -m compileall -q app tests`: passed.
- `ruff check app tests`: passed with correctness rules.
- `pip-audit -r requirements.txt`: no known vulnerabilities found.
- Structural follow-up environment: 89 tests passed, including direct route,
  quantity, messaging-idempotency and freezer-concurrency boundary tests.
- Fresh read-only production snapshot: PostgreSQL 17, 20 tables, 52,636 rows;
  isolated restore matched every table count and the baseline fingerprint.
- Migration rehearsal: version `20260803_001`, 11 validated constraints, four
  direct-SQL rejection checks, zero persistent test writes and a verified
  idempotent second application. Full evidence is in
  `docs/WAREHOUSE_SCHEMA_BASELINE_V1.md`.

## Required staging gate

1. Build the exact candidate commit from a clean checkout.
2. Use the verified production-clone evidence in
   `docs/WAREHOUSE_SCHEMA_BASELINE_V1.md`; take another fresh backup immediately
   before any later production migration.
3. Configure a strong `SECRET_KEY` and keep
   `WAREHOUSE_STRICT_STARTUP_DDL=true`.
4. Keep Telegram/daily/weekly HTTP cron callers disabled until they are changed
   atomically to `POST` plus `X-Digest-Token` or `X-Report-Token` headers.
5. Apply `20260803_001` to the isolated staging target before starting the exact
   app candidate; confirm startup completes with no DDL warnings or errors.
6. Run the critical stock, transfer, missing, consumables, PO, label and report
   characterization suite against the isolated restore.
7. Exercise two concurrent stock deductions and two concurrent consumable takes
   against PostgreSQL; confirm the second request sees the committed balance.
8. Verify login, logout and every session POST from the real staging origin.
9. Run a single header-authenticated digest/report rehearsal with providers
   redirected to approved test recipients.
10. Stop on any mismatch. Production remains unchanged until a separate exact
    commit, backup, rollback and deploy approval.

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
