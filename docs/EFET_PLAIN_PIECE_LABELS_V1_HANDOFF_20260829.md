# EFET plain-piece labels v1 — Staging handoff

## Scope

This release adds an explicit product-level classification for simple products sold by the piece,
such as chicken drumsticks. It does not add an operator-selected exemption at print time.

- Only administrators can change the classification through Product Edit.
- The classification is accepted only when the normalized product unit is `pcs`.
- Existing products remain unclassified; there is no automatic backfill.
- It waives only blank ingredients and allergen fields.
- Origin, shelf life, storage, lot, production/use-by dates, explicit approval profile and nutrition
  data or its separate documented exemption remain mandatory.
- The batch endpoint reloads the product and owns the immutable schema-v4 print snapshot. Request
  JSON cannot spoof the classification.

## Database

Migration `20260829_001` adds `products.label_plain_piece`, defaulting to `FALSE`, and the constraint
`NOT label_plain_piece OR lower(trim(unit)) = 'pcs'`. Readiness independently rejects invalid rows.

## Agent compatibility and release order

HPRT Agent `1.0.14-staging` accepts both immutable schema 3 jobs already in the queue and new schema
4 jobs. The safe Staging rollout order is strict:

1. Back up and verify the Staging database.
2. Publish and install HPRT Agent `1.0.14-staging` on the Staging print PC.
3. Prove one existing/full schema-3 compatible dry-run or queued job.
4. Deploy the Warehouse application and migration `20260829_001` to Staging.
5. Configure one test `pcs` product, print a physical label and verify all retained fields.

Do not deploy this application change before the compatible Staging agent is installed. Production is
out of scope and requires a separately versioned Production agent, physical evidence and explicit
approval.

## Automated evidence

The implementation includes server, migration, readiness, queue immutability, UI and Windows
PowerShell 5.1 renderer tests. Renderer dry-runs cover full labels, plain-piece labels, a documented
nutrition exemption, queued schema-3 compatibility and legacy single-ingredient behavior.
