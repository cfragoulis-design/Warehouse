# Warehouse Schema Baseline v1

Status: verified against a fresh production snapshot in an isolated restore
database. The migration has **not** been applied to Warehouse production and no
Warehouse service has been deployed or reconfigured.

## Exact boundary

- Source environment: Warehouse production, read only.
- Deployed source commit: `0e81e82708802ea21ba266b3d60a4a8fedf3dcc6`.
- Backup candidate checkpoint: `dd2383de87a6ab9596a64c4f3b939bddf0a36fb8`.
- Migration implementation commit:
  `4c54441cb3fdf0b67fe47608f87290a4b091e95a`.
- Fresh isolated target: `warehouse_schema_20260803_restore_verify`.
- Previous immutable evidence target `warehouse_restore_verify`: untouched.

The backup is stored only in the private local `data/backups/` boundary and is
not tracked or uploaded.

## Fresh snapshot and restore evidence

- Backup file: `warehouse-production-20260803T053919905816Z.dump`.
- SHA-256:
  `13da3ee0c9ec07e4ab6f770be1d2a6eac10655aea988fb65ae165d9ca304fc0e`.
- Size: 812,465 bytes.
- PostgreSQL major: 17.
- Public business tables: 20.
- Business rows: 52,636.
- Baseline column fingerprint:
  `f3bfacf36afaa6832d8e8812d1c6f63110500077ad61253d18b699a74dea6466`.
- Dump catalog fingerprint:
  `85b3cb099bda006f425e3c88a654fc1e74fcf84718fcd2845d7a7872341ad54b`.

The isolated restore matched the manifest's PostgreSQL major, table catalog,
schema fingerprint, every per-table row count and total row count.

## Migration `20260803_001`

The first ordered migration adds a checksummed
`warehouse_schema_migrations` registry and applies only invariants already
verified against the restored data:

- non-negative product minimum and CENTRAL target thresholds;
- positive stock-movement quantities and the four supported movement types;
- non-negative missing, freezer and consumable balances;
- supported consumable movement types, quantity semantics and non-negative
  resulting balances;
- unique `(message_id, user_id)` Workshop acknowledgements;
- `NOT NULL` alignment for `products.target_central` and
  `consumable_stock.qty`;
- normalization of 44 legacy negative `OUT` consumable magnitudes to positive
  magnitudes. Movement type, actor, timestamp, note, stock-after value and row
  identity are preserved.

The SQL executes in one transaction behind an advisory lock, with five-second
lock timeout, 60-second statement timeout, exact database-name confirmation,
baseline fingerprint guard and immutable migration checksums.

The runner has separate guarded targets for isolated restore, staging and
production databases. Staging accepts only an explicitly confirmed database
whose name ends in `_staging`; it cannot be mislabeled as a production target.

## Rehearsal result

- Migration current version: `20260803_001`.
- Post-migration column fingerprint:
  `955789c7e406e928ef79c1aad8581dbac1c7301f3c07f353aefa97d1c6ecdec8`.
- Validated new constraints: 11.
- Original business tables after migration: 20.
- Original business rows after migration: 52,636; every per-table count matched
  the source manifest.
- Direct SQL rejection verified for negative product minimum, zero stock
  movement, negative consumable balance and duplicate acknowledgement.
- All adversarial writes were rolled back; persistent test writes: 0.
- A second application was a checksum-verified no-op with the same current
  version and post-migration fingerprint.
- Local application suite: 89 passed, 6 existing SQLite date-adapter warnings.
- Ruff correctness checks: passed.

## Production gate

Production remains unchanged. Before applying this migration there, require a
separate approval that names the exact final candidate commit and includes:

1. a new production backup immediately before the maintenance window;
2. checksum and isolated restore verification;
3. a short write pause for Warehouse only;
4. guarded migration application before the matching application deploy;
5. post-migration row-count, constraint, login and critical-flow checks;
6. automatic stop before deploy if any migration or verification step fails.

SR and Sklavounos One are outside this migration and must remain unchanged.
