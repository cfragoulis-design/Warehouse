# Kiosk English + self-service PIN change — deploy runbook and rollback (2026-11-23)

Branch: `feature/kiosk-english-20261123` (from `origin/main` 5e15c83). **Not deployed.** Deploy only after the owner approves.

## What changes

* Consumables kiosk page (`/consumables/take?kiosk=1`): EN/ΕΛ toggle in the header. Take page, picker, PIN pad, session banner, messages and toasts are translated by `app/static/kiosk-i18n.js`. The script loads only in kiosk mode.
* Language rules:
  * Tapping a worker previews that worker's language.
  * After PIN login the kiosk follows the worker's `preferred_lang`.
  * When the session ends or the idle screen appears, the kiosk goes back to Greek.
* Voice: the greeting follows the worker's language. English uses `en-GB-SoniaNeural`. The take/put-back confirmation follows the page language and uses `name_en` when it is set.
  * Not changed: the kiosk-side camera reminder stays Greek, because the speaker is unknown when it fires.
* Self-service PIN change on the kiosk: tap your name, tap «Αλλαγή PIN» / "Change PIN", enter your current PIN, then the new PIN twice.
  * Trivial PINs are rejected: repeats, ascending or descending runs (also wrapping, e.g. 7890), abab and aabb patterns.
  * A new PIN equal to the old one is rejected.
  * There is **no** uniqueness check against other workers' PINs.
  * A wrong current PIN counts toward the existing 5 tries / 5 min lockout.
  * The new PIN is stored as bcrypt and never logged.
* Admin `/workers`:
  * Kiosk language per worker.
  * A "must change PIN at next login" checkbox (on by default) when a PIN is set, single or bulk.
  * A toggle to request or cancel a forced change.
  * The CLI `app.worker_pins` forces the change by default (`--no-force-change` to skip).
* Admin consumable edit page: optional English name (`name_en`). When it is empty, the Greek name is used.
* Migration `20261123_024_kiosk_english.sql` (sha256 `910e4b9b…a1673c0c08fa0ee1240ae69c3a27cabd6024b98340ccee1a3bc`), additive only:
  * `workers.preferred_lang` (default `'el'`, check el/en).
  * `workers.pin_must_change` (default false).
  * `consumables.name_en` (nullable).
  * GRANTs.
  * Existing workers stay Greek and are **not** forced to change their PIN.
* `/ready` now requires the new columns. **Migrate first, then deploy.**
* Untouched: Warehouse screens (dashboard «Κέντρο βάρδιας», stock, labels/designer, transfers and workshop loading, products). On the same database, `main` and the branch give pixel-identical screenshots for these pages (`/workspace/kiosk/out/english/probes/`). `/workers` differs only by the new admin controls.

## Verified before release

* `pytest`: 830 passed, 118 skipped. The new `tests/test_kiosk_english.py` has 36 tests. `ruff check .` is clean.
* Local prod restore (`en_try`, 22/11): the apply SQL ran twice (idempotent), `/ready` returned 200, and every flow was exercised in a browser at 1024x768.

## Deploy (owner approval required for each production step)

Run from Chris-Desktop with the Railway CLI. Use the same pattern as the 22/11 releases.

1. **Freeze the release commit**: merge or squash the branch into `main`, then note the 40-hex SHA `C`.
2. **Full backup** (read-only session). Use the `wh-predeploy6-inner/outer.ps1` pattern:
   * `pg_dump --format=custom` of the production DB.
   * `pg_restore -l` sanity check.
   * Record the row counts and sha256.
3. **Migration**: copy `warehouse-migration-kiosk-english-20261123.sql` to `%TEMP%` and run it as the DB owner:
   ```
   psql --dbname=%DATABASE_PUBLIC_URL% -v ON_ERROR_STOP=1 --single-transaction -v commit=C -f %TEMP%\warehouse-migration-kiosk-english-20261123.sql
   ```
   * It refuses unless the DB is `railway` and the ledger ends at `20261122_023`.
   * It records `20261123_024` with the checksum, then verifies it.
   * The old code keeps running happily on the new columns: it never selects them, and the inserts get the defaults.
4. **Deploy** a clean artifact of `C` (git archive, no `.venv` or tests output) with `railway up`, the same way as the last release.
   * Production predeploy runs with migrations **disabled**. It only validates the target and the approved candidate commit. Set `WAREHOUSE_CANDIDATE_COMMIT` / `WAREHOUSE_APPROVED_CANDIDATE_COMMIT` (plus tree and manifest sha from `scripts/generate_warehouse_release_manifest.py`) exactly as for the previous release.
5. **Verify**:
   * `/ready` returns 200 with schema ok.
   * Screenshot probes of `/dashboard`, `/stock`, `/movements?movement_type=TRANSFER`, `/admin/labels/designer`, `/products`, `/workers` look unchanged.
   * On the kiosk (`kiosk-apothiki`, grim screenshot): the header shows `EN`, the toggle switches EN↔ΕΛ, and the picker shows all 6 workers.
   * Do **not** try production PIN logins from the box (production is SSO). Ask a worker to log in on the kiosk instead.
6. **Optional data steps** (admin UI, no deploy):
   * `/workers`: set Kiosk language = English for Armando S. and Volodymyr K.
   * Optionally run `optional-name_en-suggestions.sql` (English names for 55 consumables; it only fills empty `name_en`) or type the names in the consumable edit page.
   * Optionally tick "must change" for workers who should replace the PIN they were given.

## Rollback

* **Code**: redeploy the previous successful deployment in Railway (as of 23/11 morning: deployment `018a9703…`, commit `00ae24f2`). Confirm the ID with `railway deployment list` first.
  * The old code does not read the ledger at runtime, and production predeploy runs with migrations disabled. So the extra ledger row `20261123_024` does not block a rollback.
  * The old `/ready` does not check the new columns.
* **Schema**: normally leave it. The three columns are additive and harmless to the old code. Only if the owner explicitly wants a full revert, after a fresh backup, in one transaction:
  ```sql
  ALTER TABLE workers DROP CONSTRAINT IF EXISTS ck_workers_preferred_lang;
  ALTER TABLE workers DROP COLUMN IF EXISTS preferred_lang, DROP COLUMN IF EXISTS pin_must_change;
  ALTER TABLE consumables DROP COLUMN IF EXISTS name_en;
  DELETE FROM warehouse_schema_migrations WHERE version = '20261123_024';
  ```
  * Do this **only** with the old code live. The new code's `/ready` fails without these columns.
  * Any PIN a worker changed on the kiosk stays valid after a rollback, because it is the same `pin_hash` column.
* **Kiosk**: nothing to roll back. The page is served by the app, and the EN toggle disappears with the old code.
