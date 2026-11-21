-- Locale-independent corrective backfill for explicit EFET approval profiles.
--
-- Some PostgreSQL installations use a C-style collation where lower() only
-- folds ASCII reliably.  The original conservative backfill therefore left
-- Greek product descriptions UNASSIGNED even though the reviewed Python
-- preview classified them.  Translate Greek uppercase characters explicitly,
-- update only still-unassigned products, and keep ambiguous rows untouched.

WITH normalized AS (
    SELECT
        id,
        translate(
            lower(concat_ws(' ', name, category, label_legal_name)),
            'ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩΆΈΉΊΌΎΏΪΫ',
            'αβγδεζηθικλμνξοπρστυφχψωάέήίόύώϊϋ'
        ) AS searchable
    FROM products
    WHERE approval_profile = 'UNASSIGNED'
), classified AS (
    SELECT
        id,
        searchable LIKE ANY (ARRAY[
            '%κοτόπου%', '%κοτοπου%', '%όρνιθ%', '%ορνιθ%',
            '%γαλοπού%', '%γαλοπου%', '%chicken%', '%poultry%', '%turkey%'
        ]) AS is_poultry,
        searchable LIKE ANY (ARRAY[
            '%μοσχ%', '%βόει%', '%βοει%', '%χοιρ%', '%αρν%',
            '%πρόβ%', '%προβ%', '%κατσίκ%', '%κατσικ%',
            '%beef%', '%veal%', '%pork%', '%lamb%', '%mutton%', '%goat%'
        ]) AS is_red_meat
    FROM normalized
), updated AS (
    UPDATE products AS product
    SET approval_profile = CASE
        WHEN classified.is_poultry THEN 'POULTRY'
        ELSE 'RED_MEAT'
    END
    FROM classified
    WHERE product.id = classified.id
      AND product.approval_profile = 'UNASSIGNED'
      AND classified.is_poultry <> classified.is_red_meat
      AND NOT EXISTS (
          SELECT 1
          FROM audit_events AS audit
          WHERE audit.entity_type = 'product'
            AND audit.entity_id = product.id::text
            AND audit.action = 'catalog.product.updated'
            AND COALESCE(
                    audit.before_json::jsonb ->> 'approval_profile',
                    'UNASSIGNED'
                ) IS DISTINCT FROM COALESCE(
                    audit.after_json::jsonb ->> 'approval_profile',
                    'UNASSIGNED'
                )
      )
    RETURNING product.id, product.approval_profile
)
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
)
SELECT
    NULL,
    'SYSTEM',
    'catalog.product.approval_profile.backfilled',
    'product',
    updated.id::text,
    jsonb_build_object('approval_profile', 'UNASSIGNED')::text,
    jsonb_build_object('approval_profile', updated.approval_profile)::text,
    'Locale-safe one-time backfill 20260828_001',
    'migration:20260828_001:' || updated.id::text
FROM updated;
