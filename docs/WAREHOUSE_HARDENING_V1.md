# Warehouse Hardening v1

Status: local verified candidate only. No staging or production deployment has
been performed.

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

## Verified evidence

- Legacy/current dependency environment: 39 tests passed.
- Isolated upgraded dependency environment: 39 tests passed.
- `python -m compileall -q app tests`: passed.
- `ruff check app tests`: passed with correctness rules.
- `pip-audit -r requirements.txt`: no known vulnerabilities found.

## Required staging gate

1. Build the exact candidate commit from a clean checkout.
2. Restore a fresh production backup into an isolated staging database.
3. Configure a strong `SECRET_KEY` and keep
   `WAREHOUSE_STRICT_STARTUP_DDL=true`.
4. Keep Telegram/daily/weekly HTTP cron callers disabled until they are changed
   atomically to `POST` plus `X-Digest-Token` or `X-Report-Token` headers.
5. Start the app against the restore and confirm startup completes with no DDL
   warnings or errors.
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

- Replace startup compatibility DDL with an exact production schema baseline
  and a versioned migration registry. This needs a fresh schema-only production
  snapshot and isolated restore verification before code can safely guess the
  baseline.
- Add persistent, expiring worker claims for label jobs. This is a schema change
  and belongs in the first versioned migration after the baseline.
- Split `services.py` into products, stock, labels and messaging routers in
  behaviour-preserving checkpoints.
- Move PIN failure counters to shared persistent storage if Warehouse is scaled
  beyond one web process/instance.
- Replace product-history `ON DELETE CASCADE` constraints after the migration
  baseline. Runtime deletion is already disabled by this checkpoint.
