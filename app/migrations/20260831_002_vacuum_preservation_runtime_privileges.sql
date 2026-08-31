-- Extend the already restricted Warehouse runtime role with the two product
-- columns needed by catalog administration. The migration runner installs the
-- exact role as a transaction-local setting after explicit confirmation.

DO $migration$
DECLARE
    runtime_role TEXT :=
        NULLIF(current_setting('warehouse.runtime_role', TRUE), '');
    runtime_oid OID;
    role_can_login BOOLEAN;
    role_is_elevated BOOLEAN;
    public_schema_oid OID := to_regnamespace('public');
    products_rel REGCLASS := to_regclass('public.products');
BEGIN
    IF runtime_role IS NULL
       OR runtime_role !~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'
       OR lower(runtime_role) = 'public' THEN
        RAISE EXCEPTION 'warehouse.runtime_role is missing or invalid';
    END IF;

    SELECT
        oid,
        rolcanlogin,
        rolsuper OR rolcreaterole OR rolcreatedb
            OR rolreplication OR rolbypassrls
    INTO runtime_oid, role_can_login, role_is_elevated
    FROM pg_catalog.pg_roles
    WHERE rolname = runtime_role;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'warehouse.runtime_role does not exist';
    END IF;

    IF NOT role_can_login OR role_is_elevated THEN
        RAISE EXCEPTION 'warehouse.runtime_role is not a restricted login role';
    END IF;

    IF runtime_role = current_user THEN
        RAISE EXCEPTION 'migration and Warehouse runtime roles must be separate';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS assumable_role
        WHERE assumable_role.oid <> runtime_oid
          AND pg_catalog.pg_has_role(runtime_oid, assumable_role.oid, 'SET')
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role must not be able to assume another role';
    END IF;

    IF public_schema_oid IS NULL OR products_rel IS NULL THEN
        RAISE EXCEPTION 'required Warehouse catalog objects are missing';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = public_schema_oid
          AND pg_catalog.pg_has_role(runtime_oid, namespace.nspowner, 'MEMBER')
    ) OR pg_catalog.has_schema_privilege(
        runtime_role,
        public_schema_oid,
        'CREATE'
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role must not own or create in the public schema';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = products_rel::oid
          AND pg_catalog.pg_has_role(runtime_oid, relation.relowner, 'MEMBER')
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role owns or can assume ownership of products';
    END IF;

    EXECUTE format(
        'GRANT UPDATE (
            vacuum_shelf_life_days,
            vacuum_storage_text
        ) ON TABLE public.products TO %I',
        runtime_role
    );

    IF NOT pg_catalog.has_column_privilege(
        runtime_role,
        products_rel,
        'vacuum_shelf_life_days',
        'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        runtime_role,
        products_rel,
        'vacuum_storage_text',
        'UPDATE'
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role is missing Vacuum catalog update access';
    END IF;

    IF pg_catalog.has_column_privilege(
        runtime_role,
        products_rel,
        'vacuum_shelf_life_days',
        'UPDATE WITH GRANT OPTION'
    ) OR pg_catalog.has_column_privilege(
        runtime_role,
        products_rel,
        'vacuum_storage_text',
        'UPDATE WITH GRANT OPTION'
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role must not hold Vacuum catalog grant options';
    END IF;
END
$migration$;
