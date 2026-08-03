# Warehouse Structure Review

Status: local review and verified code checkpoint only. No staging, production,
Operations, SR, provider or database change has been made.

## Current ownership boundaries

- Warehouse owns legacy product/location stock, transfers, missing quantities,
  label lots, freezer balances and consumables purchasing/stock.
- Consumables are already isolated from meat/product stock through dedicated
  models, movements, routes and purchase orders in `consumables_service.py`.
  They must remain a separate Operations read model and must not be mapped as
  Product Master sellable products.
- Operations consumes Warehouse data read-only. Product Master mappings remain
  explicit; Warehouse never writes into One from this application.

## Closed by the structural follow-up

- Canonical stock calculations, finite quantity parsing and missing-stock
  transitions were extracted from the monolithic router into
  `stock_domain.py`, while compatibility imports preserve current callers.
- Workshop messages were extracted into `workshop_message_service.py`. Route
  registry tests prove there is one handler per public path.
- Product and category administration were extracted into
  `catalog_service.py`; all 13 existing method/path pairs remain unique and
  category rename propagation remains transactional.
- Acknowledgements now serialize per message/user before the idempotency check.
- Freezer mutations now serialize per product and reload the current balance
  after acquiring the lock.
- Freezer routes now live in `freezer_service.py`; shared output formatting was
  moved into a dependency-free module used by both stock and freezer views.
- `services.py` was reduced from 2,700+ physical lines to 2,146 without
  changing route paths, templates or the external Operations inventory
  contract, then reduced further to 1,944 by extracting freezer routes.
- A fresh read-only production snapshot and exact isolated restore established
  the canonical PostgreSQL 17 baseline. Migration `20260803_001` was then
  applied and adversarially verified only on that clone.

## Findings that require a versioned migration

These should not be patched through startup DDL or guessed against production:

1. Production already stores `products.min_stock` as `NUMERIC(12,3)`; the stale
   integer ORM declaration was corrected without a production type change.
2. Stock, missing, freezer and consumable constraints plus acknowledgement
   uniqueness are implemented in migration `20260803_001` and verified on the
   isolated clone. They are not yet applied to production.
3. Freezer still stores only an absolute current balance. It needs an append-only
   movement ledger with actor, reason and timestamp before it can provide the
   same audit quality as normal stock.
4. Workshop acknowledgement uniqueness is covered by `20260803_001`; rollout
   remains gated with the rest of that migration.
5. Historical product foreign keys still use `ON DELETE CASCADE`. Runtime
   deletion is disabled, but the constraints should become restrictive once
   the exact production schema is baselined.
6. The ordered checksummed registry now exists. Startup compatibility DDL must
   remain only until the versioned path is proven in staging and production,
   then be removed in a later behaviour-neutral checkpoint.

## Safe next sequence

1. Preserve this local checkpoint and review only its changed/new files.
2. Use the completed exact production baseline and isolated evidence documented
   in `WAREHOUSE_SCHEMA_BASELINE_V1.md`.
3. Request separate staging approval for the exact migration/application
   candidate; do not apply it to production as part of this checkpoint.
4. Extract label routes without behaviour changes, then add persistent,
   expiring print-job claims in a later migration.
5. Introduce the freezer ledger with a guarded backfill/opening-balance event.
   Catalog and freezer route extraction are already complete.
6. Only after isolated PostgreSQL concurrency and rollback verification,
   request separate staging approval. Production remains out of scope.

## Local verification

- Complete suite: 89 passed.
- Route uniqueness verifies the extracted messaging, catalog and freezer
  modules.
- Migration `20260803_001` was executed only against
  `warehouse_schema_20260803_restore_verify`; original business row counts were
  unchanged and all adversarial writes rolled back.
- No network publication, push or deployment was performed.
