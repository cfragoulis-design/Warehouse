ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS one_sso_mappings (
    id SERIAL PRIMARY KEY,
    one_subject VARCHAR(36) NOT NULL,
    one_employee_id VARCHAR(36) NOT NULL,
    one_location_id VARCHAR(36),
    one_department_id VARCHAR(36),
    local_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    local_role VARCHAR(32) NOT NULL,
    local_location_code VARCHAR(30) NOT NULL,
    expected_email VARCHAR(320),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_one_sso_mappings_subject UNIQUE (one_subject),
    CONSTRAINT uq_one_sso_mappings_employee UNIQUE (one_employee_id),
    CONSTRAINT uq_one_sso_mappings_local_user UNIQUE (local_user_id),
    CONSTRAINT ck_one_sso_mappings_subject_uuid
        CHECK (one_subject ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT ck_one_sso_mappings_employee_uuid
        CHECK (one_employee_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT ck_one_sso_mappings_location_uuid
        CHECK (one_location_id IS NULL OR one_location_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT ck_one_sso_mappings_department_uuid
        CHECK (one_department_id IS NULL OR one_department_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT ck_one_sso_mappings_local_role
        CHECK (local_role IN ('admin', 'workshop', 'warehouse')),
    CONSTRAINT ck_one_sso_mappings_local_location
        CHECK (local_location_code IN ('ALL', 'CENTRAL', 'WORKSHOP')),
    CONSTRAINT ck_one_sso_mappings_role_location
        CHECK (
            (local_role IN ('workshop', 'warehouse')
                AND local_location_code = 'WORKSHOP'
                AND one_location_id IS NOT NULL)
            OR
            (local_role = 'admin'
                AND local_location_code = 'ALL'
                AND one_location_id IS NULL
                AND one_department_id IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS ix_one_sso_mappings_subject
    ON one_sso_mappings(one_subject);
CREATE INDEX IF NOT EXISTS ix_one_sso_mappings_employee
    ON one_sso_mappings(one_employee_id);
CREATE INDEX IF NOT EXISTS ix_one_sso_mappings_local_user
    ON one_sso_mappings(local_user_id);

CREATE OR REPLACE FUNCTION warehouse_protect_one_sso_mapping()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'One SSO mappings are append-only';
    END IF;
    IF NEW.one_subject IS DISTINCT FROM OLD.one_subject
       OR NEW.one_employee_id IS DISTINCT FROM OLD.one_employee_id
       OR NEW.one_location_id IS DISTINCT FROM OLD.one_location_id
       OR NEW.one_department_id IS DISTINCT FROM OLD.one_department_id
       OR NEW.local_user_id IS DISTINCT FROM OLD.local_user_id
       OR NEW.local_role IS DISTINCT FROM OLD.local_role
       OR NEW.local_location_code IS DISTINCT FROM OLD.local_location_code
       OR NEW.expected_email IS DISTINCT FROM OLD.expected_email
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'One SSO mapping identity and authorization are immutable';
    END IF;
    IF OLD.is_active = FALSE AND NEW.is_active = TRUE THEN
        RAISE EXCEPTION 'Inactive One SSO mappings cannot be reactivated';
    END IF;
    IF OLD.is_active = TRUE AND NEW.is_active = FALSE THEN
        INSERT INTO audit_events (
            actor_user_id,
            actor_username,
            action,
            entity_type,
            entity_id,
            before_json,
            after_json,
            reason,
            correlation_id
        ) VALUES (
            NULL,
            'SYSTEM',
            'warehouse.one_sso.mapping.deactivated',
            'one_sso_mapping',
            OLD.id::text,
            jsonb_build_object('is_active', TRUE)::text,
            jsonb_build_object('is_active', FALSE)::text,
            'Database-enforced One SSO mapping deactivation',
            'mapping-deactivate:' || OLD.id::text || ':' ||
                floor(extract(epoch FROM clock_timestamp()))::bigint::text
        );
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_one_sso_mappings_protect ON one_sso_mappings;
CREATE TRIGGER trg_one_sso_mappings_protect
BEFORE UPDATE OR DELETE ON one_sso_mappings
FOR EACH ROW EXECUTE FUNCTION warehouse_protect_one_sso_mapping();

CREATE TABLE IF NOT EXISTS one_sso_redemptions (
    id SERIAL PRIMARY KEY,
    code_digest CHAR(64) NOT NULL,
    mapping_id INTEGER NOT NULL
        REFERENCES one_sso_mappings(id) ON DELETE RESTRICT,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_one_sso_redemptions_digest UNIQUE (code_digest),
    CONSTRAINT ck_one_sso_redemptions_digest
        CHECK (code_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_one_sso_redemptions_lifetime
        CHECK (expires_at > issued_at)
);

CREATE INDEX IF NOT EXISTS ix_one_sso_redemptions_mapping
    ON one_sso_redemptions(mapping_id);
