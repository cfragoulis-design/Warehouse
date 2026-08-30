# EFET plain traceability labels — Production handoff

## Scope

This release extends the explicit product-level classification for simple internal-traceability
products. It does not add an operator-selected exemption at print time.

- Only administrators can change the classification through Product Edit.
- The classification is accepted only when the normalized product unit is `pcs`, `box` or `tray`.
- `kg` remains a normal catalog unit and cannot use this classification.
- Existing products remain unclassified; there is no automatic backfill.
- It waives only blank ingredients and allergen fields.
- Origin, shelf life, storage, lot, production/use-by dates, explicit approval profile and nutrition
  data or its separate documented exemption remain mandatory.
- The batch endpoint reloads the product and owns the immutable schema-v5 print snapshot. Request
  JSON cannot spoof the classification.

## Database

Migration `20260829_001` added the compatibility storage column. Migration `20260830_001` changes
only its unit constraint to `pcs`, `box` or `tray`; it does not update product data. Readiness
independently rejects invalid rows.

## Agent compatibility and release order

HPRT Agent `1.0.15` accepts immutable schema 3 and schema 4 jobs already in the queue, plus new
schema 5 jobs. Schema 4 keeps its original `plain_piece => pcs` contract. Schema 5 uses the canonical
`plain_traceability` key for `pcs`, `box` and `tray`. The rollout order is strict:

1. Back up and verify the database.
2. Publish and install HPRT Agent `1.0.15` on the Production print PC.
3. Prove one existing/full schema-3 compatible dry-run or queued job.
4. Deploy the Warehouse application and migration `20260830_001`.
5. Configure one test `pcs`, `box` or `tray` product, print a physical label and verify all retained fields.

Do not deploy the application change before the matching agent is installed. Production agent
`1.0.15` targets only `https://sklavounoswh.up.railway.app` and is published by the Production
release manifest. Keep `1.0.14` available as the renderer rollback artifact.

## Automated evidence

The implementation includes server, migration, readiness, queue immutability, UI and Windows
PowerShell 5.1 renderer tests. Renderer dry-runs cover full labels, schema-5 `pcs`/`box`/`tray`
labels, rejection of `kg`, a documented nutrition exemption, queued schema-3/schema-4 compatibility
and legacy single-ingredient behavior.
