-- Extend the restricted Warehouse runtime role with only the two immutable
-- content columns required when an administrator creates a new layout/content
-- version.  Existing broad-table mutation rights are neither required nor
-- granted.

DO $migration$
DECLARE
    runtime_role TEXT :=
        NULLIF(current_setting('warehouse.runtime_role', TRUE), '');
    runtime_oid OID;
    role_can_login BOOLEAN;
    role_is_elevated BOOLEAN;
    public_schema_oid OID := to_regnamespace('public');
    versions_rel REGCLASS := to_regclass('public.label_layout_versions');
    active_rel REGCLASS := to_regclass('public.label_layout_active');
    versions_seq REGCLASS;
    versions_sequence_sql TEXT;
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

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database AS database_entry
        WHERE database_entry.datname = current_database()
          AND pg_catalog.pg_has_role(
              runtime_oid,
              database_entry.datdba,
              'MEMBER'
          )
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role owns or can assume ownership of the database';
    END IF;

    IF public_schema_oid IS NULL OR versions_rel IS NULL OR active_rel IS NULL THEN
        RAISE EXCEPTION 'required label-content objects are missing';
    END IF;

    versions_seq := to_regclass(
        pg_catalog.pg_get_serial_sequence(
            'public.label_layout_versions',
            'id'
        )
    );
    IF versions_seq IS NULL THEN
        RAISE EXCEPTION 'label-layout version sequence is missing';
    END IF;

    SELECT format('%I.%I', namespace.nspname, relation.relname)
    INTO versions_sequence_sql
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE relation.oid = versions_seq::oid;

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
        WHERE relation.oid IN (
            versions_rel::oid,
            active_rel::oid,
            versions_seq::oid
        )
          AND pg_catalog.pg_has_role(runtime_oid, relation.relowner, 'MEMBER')
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role owns or can assume ownership of label content';
    END IF;

    EXECUTE format(
        'GRANT SELECT ON TABLE public.label_layout_versions TO %I',
        runtime_role
    );
    EXECUTE format(
        'GRANT INSERT (
            printer_profile,
            version,
            contract_version,
            settings_json,
            settings_sha256,
            content_json,
            content_sha256,
            based_on_version_id,
            created_by_user_id,
            change_reason
        ) ON TABLE public.label_layout_versions TO %I',
        runtime_role
    );
    EXECUTE format(
        'GRANT SELECT ON TABLE public.label_layout_active TO %I',
        runtime_role
    );
    EXECUTE format(
        'GRANT UPDATE (
            active_version_id,
            lock_version,
            updated_by_user_id,
            updated_at
        ) ON TABLE public.label_layout_active TO %I',
        runtime_role
    );
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO %I',
        versions_sequence_sql,
        runtime_role
    );

    IF NOT pg_catalog.has_table_privilege(
        runtime_role, versions_rel, 'SELECT'
    ) OR NOT pg_catalog.has_table_privilege(
        runtime_role, active_rel, 'SELECT'
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'printer_profile',
            'version',
            'contract_version',
            'settings_json',
            'settings_sha256',
            'content_json',
            'content_sha256',
            'based_on_version_id',
            'created_by_user_id',
            'change_reason'
        ]) AS allowed_column(column_name)
        WHERE NOT pg_catalog.has_column_privilege(
            runtime_role,
            versions_rel,
            allowed_column.column_name,
            'INSERT'
        )
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'active_version_id',
            'lock_version',
            'updated_by_user_id',
            'updated_at'
        ]) AS allowed_column(column_name)
        WHERE NOT pg_catalog.has_column_privilege(
            runtime_role,
            active_rel,
            allowed_column.column_name,
            'UPDATE'
        )
    ) OR NOT pg_catalog.has_sequence_privilege(
        runtime_role, versions_seq, 'USAGE'
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role is missing label-content control-plane access';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY['id', 'created_at']) AS forbidden_column(column_name)
        WHERE pg_catalog.has_column_privilege(
            runtime_role,
            versions_rel,
            forbidden_column.column_name,
            'INSERT'
        )
    ) OR pg_catalog.has_any_column_privilege(
        runtime_role, versions_rel, 'UPDATE'
    ) OR pg_catalog.has_any_column_privilege(
        runtime_role, versions_rel, 'REFERENCES'
    ) OR pg_catalog.has_any_column_privilege(
        runtime_role, active_rel, 'INSERT'
    ) OR pg_catalog.has_any_column_privilege(
        runtime_role, active_rel, 'REFERENCES'
    ) OR pg_catalog.has_column_privilege(
        runtime_role, active_rel, 'printer_profile', 'UPDATE'
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER', 'MAINTAIN'])
            AS denied(privilege_name)
        WHERE pg_catalog.has_table_privilege(
            runtime_role, versions_rel, denied.privilege_name
        ) OR pg_catalog.has_table_privilege(
            runtime_role, active_rel, denied.privilege_name
        )
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY['SELECT', 'UPDATE']) AS denied(privilege_name)
        WHERE pg_catalog.has_sequence_privilege(
            runtime_role, versions_seq, denied.privilege_name
        )
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role has forbidden label-content access';
    END IF;

    IF pg_catalog.has_table_privilege(
        runtime_role, versions_rel, 'SELECT WITH GRANT OPTION'
    ) OR pg_catalog.has_table_privilege(
        runtime_role, active_rel, 'SELECT WITH GRANT OPTION'
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'printer_profile',
            'version',
            'contract_version',
            'settings_json',
            'settings_sha256',
            'content_json',
            'content_sha256',
            'based_on_version_id',
            'created_by_user_id',
            'change_reason'
        ]) AS allowed_column(column_name)
        WHERE pg_catalog.has_column_privilege(
            runtime_role,
            versions_rel,
            allowed_column.column_name,
            'INSERT WITH GRANT OPTION'
        )
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'active_version_id',
            'lock_version',
            'updated_by_user_id',
            'updated_at'
        ]) AS allowed_column(column_name)
        WHERE pg_catalog.has_column_privilege(
            runtime_role,
            active_rel,
            allowed_column.column_name,
            'UPDATE WITH GRANT OPTION'
        )
    ) OR pg_catalog.has_sequence_privilege(
        runtime_role, versions_seq, 'USAGE WITH GRANT OPTION'
    ) THEN
        RAISE EXCEPTION
            'warehouse.runtime_role must not hold label-content grant options';
    END IF;
END
$migration$;
