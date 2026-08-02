# Warehouse Operations inventory v1 handoff

Status: local candidate, 2 August 2026. It has not been pushed, deployed or enabled in any
Warehouse or Operations environment.

## Route and activation

- `GET /api/v1/operations/inventory`.
- Requires `OPERATIONS_READ_API_ENABLED=true`, a 32+ character
  `OPERATIONS_READ_API_TOKEN`, and the independent
  `OPERATIONS_INVENTORY_READ_API_ENABLED=true` switch.
- The new switch defaults off. Disabled returns hidden `404`; missing/wrong credentials return
  `401/403`; POST returns `405`; successful responses use `Cache-Control: no-store`.
- Source-only runtime restrictions remain unchanged: startup mutations, schedulers and print
  claims must all remain disabled.

## Closed contract

The response is contract version 1, timezone-aware and bounded to 500 rows. Each row contains only:

- stable Warehouse product ID, name, SKU, category, unit and active/freezer flags;
- current CENTRAL, WORKSHOP and freezer quantities;
- total, CENTRAL target/minimum, pending, missing and low-stock state.

The endpoint contains no user, supplier, purchase price, purchase-order line, movement/lot
history, note, credential, label/print payload or mutation method. Quantity signs and derived
total/pending/low state are validated again by the Operations consumer.

## Data access

The aggregate reader currently has SELECT on six tables. Inventory v1 additionally needs SELECT
on `freezer_items`; no broader table/sequence/write grant is permitted. This is a required staging
approval and least-privilege verification gate, not an implied production permission.

## Verification completed locally

- exact current stock semantics for CENTRAL/WORKSHOP/freezer, pending, missing and low;
- inactive and freezer-only product behavior;
- naive clock and missing canonical-location failure;
- independent default-off activation and Bearer auth;
- closed response fields, no-store and GET-only routing;
- source-only runtime still mounts no UI or mutation router.

Rollback is configuration-only until an exact source commit is deployed: keep
`OPERATIONS_INVENTORY_READ_API_ENABLED=false`. Staging and production activation require separate
explicit decisions.
