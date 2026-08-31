from __future__ import annotations

from collections.abc import Sequence

import pytest

from app import schema_migrations


class RowsResult:
    def __init__(self, rows: Sequence[tuple[object, ...]]):
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class LedgerConnection:
    def __init__(self, rows: Sequence[tuple[object, ...]]):
        self.rows = rows
        self.queries: list[str] = []

    def execute(self, query: str, parameters=()) -> RowsResult:
        del parameters
        self.queries.append(query)
        if "to_regclass" in query:
            return RowsResult([(True,)])
        return RowsResult(self.rows)


class ContractConnection:
    def __init__(self, section_rows: Sequence[Sequence[tuple[object, ...]]]):
        self.section_rows = list(section_rows)
        self.queries: list[str] = []
        self.parameters: list[tuple[object, ...]] = []
        initial_overrides = {
            "search_path": '"$user", public',
            "TimeZone": "Europe/Athens",
            "DateStyle": "SQL, DMY",
            "IntervalStyle": "iso_8601",
            "extra_float_digits": "1",
            "bytea_output": "escape",
            "standard_conforming_strings": "off",
            "quote_all_identifiers": "on",
            "client_encoding": "UTF8",
        }
        self.initial_session_settings = tuple(
            initial_overrides[name]
            for name, _value in (
                schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
            )
        )
        self.session_settings = dict(
            zip(
                (
                    name
                    for name, _value in (
                        schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
                    )
                ),
                self.initial_session_settings,
                strict=True,
            )
        )

    def execute(self, query: str, parameters=()) -> RowsResult:
        self.queries.append(query)
        self.parameters.append(tuple(parameters))
        if "set_config" in query:
            pairs = tuple(zip(parameters[::2], parameters[1::2], strict=True))
            for name, value in pairs:
                self.session_settings[str(name)] = str(value)
            return RowsResult([tuple(str(value) for _name, value in pairs)])
        if "current_setting" in query:
            return RowsResult(
                [
                    tuple(
                        self.session_settings[str(name)] for name in parameters
                    )
                ]
            )
        return RowsResult(self.section_rows.pop(0))


class ProtectionConnection:
    def __init__(self, rows: Sequence[tuple[object, ...]]):
        self.rows = rows
        self.calls = 0

    def execute(self, query: str, parameters=()) -> RowsResult:
        assert "pg_catalog.pg_trigger" in query
        assert parameters
        self.calls += 1
        return RowsResult(self.rows)


def _migration(
    version: str,
    *,
    filename: str | None = None,
    checksum: str | None = None,
) -> schema_migrations.MigrationDefinition:
    return schema_migrations.MigrationDefinition(
        version=version,
        filename=filename or f"{version}_change.sql",
        checksum=checksum or version.replace("_", "").ljust(64, "0")[:64],
        sql="SELECT 1",
    )


def test_catalog_requires_unique_strictly_ordered_versions_and_files() -> None:
    valid = (_migration("20260830_001"), _migration("20260830_002"))
    schema_migrations._validate_migration_catalog(valid)

    with pytest.raises(RuntimeError, match="duplicate versions"):
        schema_migrations._validate_migration_catalog((valid[0], valid[0]))
    with pytest.raises(RuntimeError, match="strictly ordered"):
        schema_migrations._validate_migration_catalog(tuple(reversed(valid)))
    with pytest.raises(RuntimeError, match="duplicate filenames"):
        schema_migrations._validate_migration_catalog(
            (
                valid[0],
                _migration("20260830_002", filename=valid[0].filename),
            )
        )
    with pytest.raises(RuntimeError, match="invalid version"):
        schema_migrations._validate_migration_catalog(
            (_migration("20260830_1"),)
        )


def test_applied_ledger_must_be_the_exact_ordered_catalog_prefix() -> None:
    catalog = (
        _migration("20260830_001", checksum="a" * 64),
        _migration("20260830_002", checksum="b" * 64),
        _migration("20260830_003", checksum="c" * 64),
    )
    schema_migrations._validate_applied_catalog(
        applied={
            "20260830_001": "a" * 64,
            "20260830_002": "b" * 64,
        },
        catalog=catalog,
    )

    invalid_ledgers = (
        {"20260830_002": "b" * 64},
        {
            "20260830_002": "b" * 64,
            "20260830_001": "a" * 64,
        },
        {
            "20260830_001": "a" * 64,
            "20260830_003": "c" * 64,
        },
        {"20260830_000": "d" * 64},
    )
    for applied in invalid_ledgers:
        with pytest.raises(RuntimeError, match="exact ordered catalog prefix"):
            schema_migrations._validate_applied_catalog(
                applied=applied,
                catalog=catalog,
            )

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        schema_migrations._validate_applied_catalog(
            applied={"20260830_001": "f" * 64},
            catalog=catalog,
        )


def test_ledger_reader_preserves_application_order_and_rejects_duplicates() -> None:
    connection = LedgerConnection(
        [
            ("20260803_001", "a" * 64),
            ("20260823_001", "b" * 64),
        ]
    )
    applied = schema_migrations._applied_migrations(connection)

    assert tuple(applied) == ("20260803_001", "20260823_001")
    assert "ORDER BY version" in connection.queries[-1]

    duplicate_connection = LedgerConnection(
        [
            ("20260803_001", "a" * 64),
            ("20260803_001", "a" * 64),
        ]
    )
    with pytest.raises(RuntimeError, match="duplicate versions"):
        schema_migrations._applied_migrations(duplicate_connection)


def test_only_exact_hash_pinned_production_gap_can_be_diagnosed() -> None:
    catalog = schema_migrations.migration_catalog()
    applied = dict(schema_migrations._PRODUCTION_DEFERRED_ONE_SSO_APPLIED)

    repair = schema_migrations.diagnose_production_deferred_one_sso_ledger(
        applied=applied,
        catalog=catalog,
    )

    assert repair.exact_applied_versions == tuple(applied)
    assert repair.deferred_migration.version == "20260828_002"
    assert (
        repair.deferred_migration.checksum
        == schema_migrations._PRODUCTION_DEFERRED_ONE_SSO_CHECKSUM
    )
    with pytest.raises(RuntimeError, match="reviewed deferred One SSO history"):
        schema_migrations.diagnose_production_deferred_one_sso_ledger(
            applied={**applied, "20260830_002": catalog[-2].checksum},
            catalog=catalog,
        )
    changed = dict(applied)
    changed["20260827_001"] = "f" * 64
    with pytest.raises(RuntimeError, match="reviewed deferred One SSO history"):
        schema_migrations.diagnose_production_deferred_one_sso_ledger(
            applied=changed,
            catalog=catalog,
        )


def test_deferred_production_gap_is_rejected_by_ordinary_validation() -> None:
    with pytest.raises(RuntimeError, match="exact ordered catalog prefix"):
        schema_migrations._validate_applied_catalog(
            applied=dict(schema_migrations._PRODUCTION_DEFERRED_ONE_SSO_APPLIED),
            catalog=schema_migrations.migration_catalog(),
        )


def _empty_contract_sections() -> list[list[tuple[object, ...]]]:
    return [[] for _section in range(10)]


def test_v2_fingerprint_covers_complete_public_schema_contract() -> None:
    sections = _empty_contract_sections()
    sections[0] = [("products", "r", "p")]
    sections[2] = [("products", "ck_products_unit", "c", "CHECK (...)")]
    first = ContractConnection(sections)
    first_hash = schema_migrations._schema_fingerprint(first)

    changed_sections = _empty_contract_sections()
    changed_sections[0] = [("products", "r", "p")]
    changed_sections[2] = [
        ("products", "ck_products_unit", "c", "CHECK (unit <> '')")
    ]
    changed_hash = schema_migrations._schema_fingerprint(
        ContractConnection(changed_sections)
    )

    assert len(first_hash) == 64
    assert first_hash != changed_hash
    all_queries = "\n".join(first.queries)
    for catalog_name in (
        "pg_catalog.pg_constraint",
        "pg_catalog.pg_index",
        "pg_catalog.pg_trigger",
        "pg_catalog.pg_policy",
        "pg_catalog.pg_get_viewdef",
        "pg_catalog.pg_proc",
        "pg_catalog.pg_sequence",
    ):
        assert catalog_name in all_queries
    section_parameters = tuple(
        parameters
        for query, parameters in zip(first.queries, first.parameters, strict=True)
        if "set_config" not in query and "current_setting" not in query
    )
    assert all(
        all(value == schema_migrations.MIGRATION_TABLE for value in parameters)
        for parameters in section_parameters
    )
    set_config_parameters = tuple(
        parameters
        for query, parameters in zip(first.queries, first.parameters, strict=True)
        if "set_config" in query
    )
    normalized_parameters = set_config_parameters[0]
    assert normalized_parameters == tuple(
        value
        for setting in schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
        for value in setting
    )
    assert first.session_settings == dict(
        zip(
            (
                name
                for name, _value in (
                    schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
                )
            ),
            first.initial_session_settings,
            strict=True,
        )
    )


def test_v2_fingerprint_fails_closed_when_session_cannot_be_normalized() -> None:
    class BadSessionConnection(ContractConnection):
        def execute(self, query: str, parameters=()) -> RowsResult:
            if "set_config" in query:
                return RowsResult(
                    [
                        ("wrong",)
                        * len(
                            schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
                        )
                    ]
                )
            return super().execute(query, parameters)

    with pytest.raises(RuntimeError, match="session could not be normalized"):
        schema_migrations._schema_fingerprint(
            BadSessionConnection(_empty_contract_sections())
        )

    autocommit = ContractConnection(_empty_contract_sections())
    autocommit.autocommit = True
    with pytest.raises(RuntimeError, match="requires an explicit transaction"):
        schema_migrations._schema_fingerprint(autocommit)


def test_schema_contract_fingerprint_is_explicitly_versioned() -> None:
    result = schema_migrations.schema_contract_fingerprint(
        ContractConnection(_empty_contract_sections())
    )

    assert result.version == "warehouse-schema-contract-v3"
    assert len(result.sha256) == 64
    assert (
        schema_migrations.LEGACY_BASELINE_FINGERPRINT_VERSION
        == "warehouse-columns-v1"
    )


def test_schema_contract_canonicalizes_pg17_varchar_array_round_trip() -> None:
    source = _empty_contract_sections()
    source[2] = [
        (
            "products",
            "ck_products_approval_profile",
            "c",
            "CHECK (approval_profile::text = ANY "
            "(ARRAY['POULTRY'::character varying, "
            "'RED_MEAT'::character varying]::text[]))",
        )
    ]
    restored = _empty_contract_sections()
    restored[2] = [
        (
            "products",
            "ck_products_approval_profile",
            "c",
            "CHECK (approval_profile::text = ANY "
            "(ARRAY['POULTRY'::character varying::text, "
            "'RED_MEAT'::character varying::text]))",
        )
    ]

    assert schema_migrations._schema_fingerprint(
        ContractConnection(source)
    ) == schema_migrations._schema_fingerprint(ContractConnection(restored))


@pytest.mark.parametrize(
    ("production", "restored"),
    (
        (
            "CHECK (((((movement_type)::text = ANY "
            "((ARRAY['IN'::character varying, "
            "'OUT'::character varying])::text[])) AND "
            "(qty > (0)::numeric)) OR (((movement_type)::text = "
            "'ADJUST'::text) AND (qty <> (0)::numeric))))",
            "CHECK (((((movement_type)::text = ANY "
            "(ARRAY[('IN'::character varying)::text, "
            "('OUT'::character varying)::text])) AND "
            "(qty > (0)::numeric)) OR (((movement_type)::text = "
            "'ADJUST'::text) AND (qty <> (0)::numeric))))",
        ),
        (
            "CHECK (((movement_type)::text = ANY "
            "((ARRAY['IN'::character varying, 'OUT'::character varying, "
            "'ADJUST'::character varying])::text[])))",
            "CHECK (((movement_type)::text = ANY "
            "(ARRAY[('IN'::character varying)::text, "
            "('OUT'::character varying)::text, "
            "('ADJUST'::character varying)::text])))",
        ),
        (
            "CHECK (((approval_profile)::text = ANY "
            "((ARRAY['POULTRY'::character varying, "
            "'RED_MEAT'::character varying, "
            "'UNASSIGNED'::character varying])::text[])))",
            "CHECK (((approval_profile)::text = ANY "
            "(ARRAY[('POULTRY'::character varying)::text, "
            "('RED_MEAT'::character varying)::text, "
            "('UNASSIGNED'::character varying)::text])))",
        ),
        (
            "CHECK (((movement_type)::text = ANY "
            "((ARRAY['IN'::character varying, 'OUT'::character varying, "
            "'ADJ+'::character varying, "
            "'ADJ-'::character varying])::text[])))",
            "CHECK (((movement_type)::text = ANY "
            "(ARRAY[('IN'::character varying)::text, "
            "('OUT'::character varying)::text, "
            "('ADJ+'::character varying)::text, "
            "('ADJ-'::character varying)::text])))",
        ),
    ),
)
def test_constraint_canonicalizer_matches_all_observed_pg17_round_trips(
    production: str,
    restored: str,
) -> None:
    assert schema_migrations.canonicalize_constraint_definition(
        production
    ) == schema_migrations.canonicalize_constraint_definition(restored)


def test_column_contract_ignores_physical_missing_values_but_hashes_defaults() -> None:
    baseline = _empty_contract_sections()
    baseline[1] = [
        (
            "products",
            "approval_profile",
            "3",
            "character varying(16)",
            "True",
            "",
            "",
            "x",
            "",
            "-1",
            "0",
            "pg_catalog.default",
            "'UNASSIGNED'::character varying",
        )
    ]
    changed_default = _empty_contract_sections()
    changed_default[1] = [
        (*baseline[1][0][:-1], "'POULTRY'::character varying")
    ]
    connection = ContractConnection(baseline)
    baseline_fingerprint = schema_migrations._schema_fingerprint(connection)
    column_query = next(
        query for query in connection.queries if "pg_catalog.pg_attribute" in query
    )

    assert "atthasmissing" not in column_query
    assert "attmissingval" not in column_query
    assert "pg_catalog.pg_get_expr" in column_query
    assert baseline_fingerprint != schema_migrations._schema_fingerprint(
        ContractConnection(changed_default)
    )


def test_constraint_canonicalizer_keeps_other_casts_and_meaning_changes() -> None:
    varchar = "CHECK (x::character varying = 'a'::character varying)"
    text = "CHECK (x::text = 'a'::text)"
    scalar_parenthesized = "CHECK (x::text = ('a'::character varying)::text)"
    strict = "CHECK (x > 0)"
    relaxed = "CHECK (x >= 0)"

    assert schema_migrations.canonicalize_constraint_definition(varchar) == varchar
    assert schema_migrations.canonicalize_constraint_definition(text) == text
    assert (
        schema_migrations.canonicalize_constraint_definition(scalar_parenthesized)
        == scalar_parenthesized
    )
    assert schema_migrations.canonicalize_constraint_definition(strict) != (
        schema_migrations.canonicalize_constraint_definition(relaxed)
    )


def test_empty_ledger_baseline_uses_only_the_versioned_legacy_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyConnection:
        def __init__(self) -> None:
            self.session_settings = {
                name: value
                for name, value in (
                    schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
                )
            }

        def execute(self, query: str, parameters=()) -> RowsResult:
            if "set_config" in query:
                pairs = tuple(zip(parameters[::2], parameters[1::2], strict=True))
                for name, value in pairs:
                    self.session_settings[str(name)] = str(value)
                return RowsResult([tuple(str(value) for _name, value in pairs)])
            if "current_setting" in query:
                return RowsResult(
                    [tuple(self.session_settings[str(name)] for name in parameters)]
                )
            return RowsResult([("products", "id", 1, "integer")])

    connection = LegacyConnection()
    reviewed = schema_migrations._legacy_schema_fingerprint(connection)
    monkeypatch.setattr(
        schema_migrations,
        "LEGACY_BASELINE_SCHEMA_FINGERPRINT",
        reviewed,
    )
    assert (
        schema_migrations.validate_legacy_empty_ledger_baseline(connection)
        == reviewed
    )
    monkeypatch.setattr(
        schema_migrations,
        "LEGACY_BASELINE_SCHEMA_FINGERPRINT",
        "f" * 64,
    )
    with pytest.raises(RuntimeError, match="reviewed baseline"):
        schema_migrations.validate_legacy_empty_ledger_baseline(connection)


def test_v2_fingerprint_normalizes_row_order() -> None:
    forward = _empty_contract_sections()
    forward[0] = [("products",), ("users",)]
    reverse = _empty_contract_sections()
    reverse[0] = list(reversed(forward[0]))

    assert schema_migrations._schema_fingerprint(
        ContractConnection(forward)
    ) == schema_migrations._schema_fingerprint(ContractConnection(reverse))


def _protected_rows() -> list[tuple[object, ...]]:
    catalog = schema_migrations.migration_catalog()

    def body(version: str, function: str) -> str:
        return schema_migrations._expected_protection_function_body(
            catalog=catalog,
            migration_version=version,
            function_name=function,
        )

    return [
        (
            "audit_events",
            "r",
            "trg_audit_events_append_only",
            "O",
            False,
            27,
            "",
            "",
            "",
            "public",
            "warehouse_reject_audit_event_mutation",
            "plpgsql",
            "f",
            "",
            "trigger",
            "v",
            False,
            False,
            False,
            "u",
            "",
            body(
                "20260827_001",
                "warehouse_reject_audit_event_mutation",
            ),
        ),
        (
            "label_layout_active",
            "r",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "label_layout_versions",
            "r",
            "trg_label_layout_versions_append_only",
            "O",
            False,
            27,
            "",
            "",
            "",
            "public",
            "warehouse_reject_label_layout_version_mutation",
            "plpgsql",
            "f",
            "",
            "trigger",
            "v",
            False,
            False,
            False,
            "u",
            "",
            body(
                "20260830_002",
                "warehouse_reject_label_layout_version_mutation",
            ),
        ),
        (
            "one_sso_mappings",
            "r",
            "trg_one_sso_mappings_protect",
            "O",
            False,
            27,
            "",
            "",
            "",
            "public",
            "warehouse_protect_one_sso_mapping",
            "plpgsql",
            "f",
            "",
            "trigger",
            "v",
            False,
            False,
            False,
            "u",
            "",
            body("20260828_002", "warehouse_protect_one_sso_mapping"),
        ),
        (
            "one_sso_redemptions",
            "r",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "product_lots",
            "r",
            "trg_product_lots_label_payload_immutable",
            "O",
            False,
            19,
            "label_payload_json",
            "",
            "",
            "public",
            "warehouse_reject_label_payload_mutation",
            "plpgsql",
            "f",
            "",
            "trigger",
            "v",
            False,
            False,
            False,
            "u",
            "",
            body(
                "20260830_002",
                "warehouse_reject_label_payload_mutation",
            ),
        ),
    ]


def test_recorded_migrations_require_their_tables_and_active_protections() -> None:
    catalog = schema_migrations.migration_catalog()
    versions = tuple(migration.version for migration in catalog[:-1])
    connection = ProtectionConnection(_protected_rows())

    verified = schema_migrations.validate_recorded_migration_protections(
        connection,
        applied_versions=versions,
    )

    assert verified == (
        "audit_events",
        "label_layout_active",
        "label_layout_versions",
        "one_sso_mappings",
        "one_sso_redemptions",
        "product_lots",
    )
    assert connection.calls == 1


def test_recorded_migration_protection_rejects_missing_or_disabled_objects() -> None:
    versions = tuple(
        migration.version
        for migration in schema_migrations.migration_catalog()
        if migration.version <= "20260827_001"
    )
    with pytest.raises(RuntimeError, match="missing protected table"):
        schema_migrations.validate_recorded_migration_protections(
            ProtectionConnection([]),
            applied_versions=versions,
        )

    disabled = [
        (
            "audit_events",
            "r",
            "trg_audit_events_append_only",
            "D",
            False,
            27,
            "",
            "",
            "",
            "public",
            "warehouse_reject_audit_event_mutation",
            "plpgsql",
            "f",
            "",
            "trigger",
            "v",
            False,
            False,
            False,
            "u",
            "",
            "BEGIN RAISE EXCEPTION 'audit_events is append-only'; END",
        )
    ]
    with pytest.raises(RuntimeError, match="active protection trigger"):
        schema_migrations.validate_recorded_migration_protections(
            ProtectionConnection(disabled),
            applied_versions=versions,
        )


def test_recorded_protection_rejects_same_name_noop_function_and_wrong_event() -> None:
    catalog = schema_migrations.migration_catalog()
    versions = tuple(
        migration.version
        for migration in catalog
        if migration.version <= "20260827_001"
    )
    valid = _protected_rows()[0]
    noop_function = (*valid[:-1], "BEGIN RETURN NEW; END")
    with pytest.raises(RuntimeError, match="protection contract has drifted"):
        schema_migrations.validate_recorded_migration_protections(
            ProtectionConnection([noop_function]),
            applied_versions=versions,
        )

    wrong_event = (*valid[:5], 7, *valid[6:])
    with pytest.raises(RuntimeError, match="protection contract has drifted"):
        schema_migrations.validate_recorded_migration_protections(
            ProtectionConnection([wrong_event]),
            applied_versions=versions,
        )

    changed_fire_mode = (*valid[:3], "A", *valid[4:])
    with pytest.raises(RuntimeError, match="protection contract has drifted"):
        schema_migrations.validate_recorded_migration_protections(
            ProtectionConnection([changed_fire_mode]),
            applied_versions=versions,
        )
