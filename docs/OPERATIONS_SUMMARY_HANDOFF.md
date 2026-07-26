# Warehouse Operations Summary Handoff

Status date: 26 July 2026.

This checkpoint adds the first aggregate-only Warehouse source contract required by Sklavounos
Operations. It is local, disabled by default and has not been pushed or deployed.

## Boundary

- Route: `GET /api/v1/operations/summary`
- Authentication: `Authorization: Bearer <service token>`
- Activation requires both `OPERATIONS_READ_API_ENABLED=true` and an
  `OPERATIONS_READ_API_TOKEN` of at least 32 characters.
- A disabled or unsafely configured route returns hidden `404`.
- Missing credentials return `401`; a wrong credential returns `403`.
- No POST, mutation, product row, supplier identity, movement history, PIN, provider credential
  or print-agent data is exposed.
- The response uses `Cache-Control: no-store`.

The Warehouse UI, session/PIN authentication, schedulers, reports, print agents and all existing
business routes are unchanged.

## Closed response

```json
{
  "as_of": "2026-07-26T07:06:16.000000Z",
  "active_products": 1,
  "low_stock_products": 1,
  "missing_products": 1,
  "production_today": 1,
  "purchase_orders_open": 1
}
```

Metric semantics:

- `active_products`: all active Warehouse products, including freezer-only products;
- `low_stock_products`: active, non-freezer-only products whose CENTRAL stock is below a positive
  `min_stock`, matching the visible Warehouse `LOW` rule;
- `missing_products`: active products with a positive persisted missing/owed quantity;
- `production_today`: product lots whose production date is today in `Europe/Athens`;
- `purchase_orders_open`: purchase orders in `DRAFT`, `SUBMITTED` or `PARTIAL`.

`as_of` is timezone-aware UTC. A missing canonical `CENTRAL` location or database failure returns
a generic `503`; internal database details are not returned.

## Verification

Focused verification:

- Ruff: pass.
- Pytest: 5 passed.
- Exact field and count semantics on disposable SQLite: pass.
- Disabled/short-token hidden route: pass.
- Missing/wrong/correct Bearer credential: pass.
- GET-only boundary and `Cache-Control: no-store`: pass.

An additional real loopback HTTP exercise connected the new source endpoint to the existing
Operations `WarehouseReadAdapter` and cached read-model service:

1. a valid sync returned `CURRENT`;
2. the cached payload contained exactly the five declared aggregates;
3. the source server was stopped completely;
4. a second sync returned `FAILED` with the bounded error
   `Read integration request failed`;
5. the exact last-good payload and source timestamp remained unchanged;
6. the temporary source/destination databases were removed and no persistent configuration or
   external system was contacted.

The exercise also returned `401` without auth, `403` for a wrong token and `405` for POST.

## Rollback and activation

Rollback is configuration-only because this checkpoint adds no schema or stored data:

1. set `OPERATIONS_READ_API_ENABLED=false`;
2. remove `OPERATIONS_READ_API_TOKEN`;
3. redeploy the prior exact Warehouse commit if the route itself must be absent.

No Operations staging connection should be configured until the Warehouse staging/non-production
target, backup operator, credential rotation owner and explicit read-only connection approval are
recorded. Production remains a separate decision.

## Changed/new files

- `app/app.py`
- `app/operations_summary.py`
- `tests/test_operations_summary.py`
- `requirements-dev.txt`
- `README.md`
- `docs/OPERATIONS_SUMMARY_HANDOFF.md`

