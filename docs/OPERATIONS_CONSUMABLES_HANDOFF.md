# Warehouse Operations consumables v1 handoff

Status: local default-off candidate, 3 August 2026. It has not been pushed, deployed or enabled
in Warehouse, Operations, SR or any managed environment.

## Ownership decision

Consumables remain an independent Warehouse-owned ledger. They are not products for sale, do
not enter Sklavounos One Product Master and do not share product mappings. This candidate only
projects a bounded operational view into a separate One read model.

## Route and activation

- Route: `GET /api/v1/operations/consumables`.
- Base read boundary: `OPERATIONS_READ_API_ENABLED=true` and the existing 32+ character Bearer
  token.
- Independent route boundary: `OPERATIONS_CONSUMABLES_READ_API_ENABLED=true`.
- The new switch defaults off. Disabled returns hidden `404`; missing/wrong credentials return
  `401/403`; POST returns `405`; successful responses use `Cache-Control: no-store`.
- The fail-closed deployment guard rejects any configuration that enables the Consumables route
  without the base Operations read API. Its privacy-safe report exposes only the boolean state,
  never a token, database URL or business value.
- No Warehouse UI, session, mutation, scheduler or print route is added.

## Closed contract

Contract version 1 contains at most 500 rows. Each row exposes only:

- stable consumable ID, name, optional category/unit and active state;
- current WORKSHOP quantity, minimum and desired quantities;
- outstanding quantity on open purchase orders;
- derived suggested-order quantity and low-stock state.

CENTRAL stock is deliberately not folded into the WORKSHOP value. Supplier identities, costs,
pack costs, notes, movement history, purchase-order identity/status, users and credentials are
excluded. Derived quantities and state are validated again by the One consumer.

## Activation boundary

The source query needs SELECT only on the existing `consumables`, `consumable_stock`,
`purchase_orders` and `purchase_order_items` tables. Any staging activation must first verify the
isolated bridge role has only the exact necessary SELECT grants and no mutation capability.
Production remains unauthorized until a fresh backup/isolated restore, exact-commit release,
failure/recovery, last-good and scheduled-observation gate is separately approved and recorded.

Rollback before activation is configuration-only: leave
`OPERATIONS_CONSUMABLES_READ_API_ENABLED=false` or unset.
