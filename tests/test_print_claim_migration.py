from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"
UP = MIGRATIONS / "011_add_print_claim_leases.sql"
DOWN = MIGRATIONS / "011_drop_print_claim_leases.sql"


def test_print_claim_upgrade_is_locked_versioned_and_fail_closed() -> None:
    sql = UP.read_text(encoding="utf-8")

    assert "BEGIN;" in sql
    assert "LOCK TABLE product_lots" in sql
    assert "ADD COLUMN IF NOT EXISTS lease_token" in sql
    assert "ADD COLUMN IF NOT EXISTS claim_started_at" in sql
    assert "ADD COLUMN IF NOT EXISTS lease_expires_at" in sql
    assert "ck_product_lots_print_claim_lease" in sql
    assert "lease_expires_at > claim_started_at" in sql
    assert "VALIDATE CONSTRAINT" in sql
    assert "ix_product_lots_station_status_lease" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_print_claim_downgrade_refuses_active_work_and_removes_every_object() -> None:
    sql = DOWN.read_text(encoding="utf-8")

    assert "LOCK TABLE product_lots" in sql
    assert "status = 'PROCESSING'" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP INDEX IF EXISTS ix_product_lots_station_status_lease" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_product_lots_print_claim_lease" in sql
    assert "DROP COLUMN IF EXISTS lease_expires_at" in sql
    assert "DROP COLUMN IF EXISTS claim_started_at" in sql
    assert "DROP COLUMN IF EXISTS lease_token" in sql
    assert sql.rstrip().endswith("COMMIT;")
