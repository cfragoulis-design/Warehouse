-- A category can have its own photo. Item photos stay the fallback.

CREATE TABLE IF NOT EXISTS consumable_category_images (
    id SERIAL PRIMARY KEY,
    category_name VARCHAR(120) NOT NULL UNIQUE,
    image_bytes BYTEA,
    image_content_type VARCHAR(40)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_category_images TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_category_images_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_category_images TO warehouse_operations_prod_reader;
