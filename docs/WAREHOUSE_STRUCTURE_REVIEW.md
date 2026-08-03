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
- `services.py` was reduced from 2,700+ physical lines to 2,146 without
  changing route paths, templates or the external Operations inventory
  contract.

## Findings that require a versioned migration

These should not be patched through startup DDL or guessed against production:

1. `products.min_stock` is an integer although kilogram products can need a
   fractional threshold. It should become `NUMERIC(12,3)` in a verified
   migration.
2. Stock quantities and movement types have no database `CHECK` constraints.
   Web validation is present, but direct SQL can still violate the domain.
3. Freezer stores only an absolute current balance. It needs an append-only
   movement ledger with actor, reason and timestamp before it can provide the
   same audit quality as normal stock.
4. Workshop acknowledgement idempotency is protected in the application but
   lacks a unique `(message_id, user_id)` database constraint.
5. Historical product foreign keys still use `ON DELETE CASCADE`. Runtime
   deletion is disabled, but the constraints should become restrictive once
   the exact production schema is baselined.
6. Startup compatibility DDL and legacy SQL files are not a complete ordered
   migration registry. A schema-only production snapshot and isolated restore
   must establish the baseline first.

## Safe next sequence

1. Preserve this local checkpoint and review only its changed/new files.
2. Establish the exact production schema baseline from a read-only snapshot in
   an isolated database.
3. Create and rehearse the first ordered Warehouse migration containing the
   numeric threshold, database invariants and acknowledgement uniqueness.
4. Extract label routes without behaviour changes, then add persistent,
   expiring print-job claims in a later migration.
5. Extract the freezer routes; introduce the freezer ledger with a guarded
   backfill/opening-balance event. Catalog extraction is already complete.
6. Only after isolated PostgreSQL concurrency and rollback verification,
   request separate staging approval. Production remains out of scope.

## Local verification

- Complete suite: 84 passed.
- Route uniqueness verifies the extracted messaging and catalog modules.
- No schema migration was added or executed.
- No network publication, push or deployment was performed.
