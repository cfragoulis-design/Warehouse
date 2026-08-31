from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app import schema_migrations
from scripts import warehouse_production_release_job as release_job


def _restore_url() -> str:
    value = os.getenv("WAREHOUSE_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("WAREHOUSE_TEST_POSTGRES_URL is not configured")
    database = str(make_url(value).database or "")
    if not database.endswith(schema_migrations.RESTORE_DATABASE_SUFFIX):
        pytest.fail("PostgreSQL integration URL must target a *_restore_verify database")
    return value


def test_scram_create_role_is_valid_postgresql_17_sql_and_rolls_back() -> None:
    url = _restore_url()
    role = release_job.PRODUCTION_RUNTIME_ROLE
    password = "Restore-only-SCRAM-password-with-32-chars!"
    with psycopg.connect(url, autocommit=False) as connection:
        version = connection.execute("SHOW server_version_num").fetchone()
        assert version is not None and int(version[0]) >= 170000
        assert connection.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)
        ).fetchone() is None
        release_job._create_runtime_role(connection, password=password)
        stored = connection.execute(
            "SELECT rolpassword FROM pg_catalog.pg_authid WHERE rolname = %s",
            (role,),
        ).fetchone()
        assert stored is not None
        assert str(stored[0]).startswith("SCRAM-SHA-256$4096:")
        assert password not in str(stored[0])
        connection.rollback()
    with psycopg.connect(url, autocommit=False) as verification:
        assert verification.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)
        ).fetchone() is None
        verification.rollback()


def test_postgresql_transaction_failure_rolls_back_all_primitives() -> None:
    url = _restore_url()
    table = f"release_rollback_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=False) as connection:
        with pytest.raises(psycopg.Error):
            connection.execute(
                psycopg.sql.SQL("CREATE TABLE public.{} (id integer PRIMARY KEY)").format(
                    psycopg.sql.Identifier(table)
                )
            )
            connection.execute(
                psycopg.sql.SQL("INSERT INTO public.{} VALUES (1), (1)").format(
                    psycopg.sql.Identifier(table)
                )
            )
        connection.rollback()
    with psycopg.connect(url, autocommit=False) as verification:
        assert verification.execute(
            "SELECT to_regclass(%s)", (f"public.{table}",)
        ).fetchone() == (None,)
        verification.rollback()


def test_global_acl_inventory_observes_direct_public_and_default_acl_sources() -> None:
    url = _restore_url()
    runtime = release_job.PRODUCTION_RUNTIME_ROLE
    reader = release_job.PRODUCTION_READER_ROLE
    schema = f"acl_probe_{uuid.uuid4().hex[:12]}"
    table = f"inventory_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, autocommit=False) as connection:
        current_user = str(connection.execute("SELECT current_user").fetchone()[0])
        for role in (runtime, reader):
            connection.execute(
                psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(
                    psycopg.sql.Identifier(role)
                )
            )
        connection.execute(
            psycopg.sql.SQL("CREATE SCHEMA {} AUTHORIZATION CURRENT_USER").format(
                psycopg.sql.Identifier(schema)
            )
        )
        connection.execute(
            psycopg.sql.SQL("CREATE TABLE {}.{} (id integer, note text)").format(
                psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table)
            )
        )
        connection.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO PUBLIC").format(
                psycopg.sql.Identifier(schema)
            )
        )
        connection.execute(
            psycopg.sql.SQL("GRANT SELECT ON TABLE {}.{} TO {}").format(
                psycopg.sql.Identifier(schema),
                psycopg.sql.Identifier(table),
                psycopg.sql.Identifier(reader),
            )
        )
        connection.execute(
            psycopg.sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                psycopg.sql.Identifier(schema),
                psycopg.sql.Identifier(runtime),
            )
        )

        effective = release_job._effective_role_privileges(connection)
        direct = release_job._direct_role_object_privileges(connection)
        public = release_job._public_object_privileges(connection)
        defaults = release_job._unsafe_default_acl_entries(connection)

        assert any(
            row[:3] == ("schema", schema, schema)
            and row[4:6] == (reader, "USAGE")
            for row in effective
        )
        assert any(
            row[:3] == ("relation", schema, table)
            and row[4:6] == (reader, "SELECT")
            for row in direct
        )
        assert any(
            row[:3] == ("schema", schema, schema)
            and row[4] == "USAGE"
            for row in public
        )
        assert any(
            row[0] == current_user
            and row[1] == schema
            and row[3] == runtime
            and row[4] == "SELECT"
            for row in defaults
        )
        connection.rollback()
