from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import inspect, text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.approval_profiles import build_backfill_preview  # noqa: E402
from app.db import engine  # noqa: E402


def main() -> int:
    columns = {column["name"] for column in inspect(engine).get_columns("products")}
    approval_expression = (
        "approval_profile"
        if "approval_profile" in columns
        else "'UNASSIGNED' AS approval_profile"
    )
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, name, category, label_legal_name, "
                f"{approval_expression} FROM products ORDER BY id"
            )
        ).mappings()
        products = tuple(SimpleNamespace(**dict(row)) for row in rows)

    preview = build_backfill_preview(products)
    counts = Counter(item.proposed_profile for item in preview)
    payload = {
        "read_only": True,
        "summary": dict(sorted(counts.items())),
        "requires_review": [
            {
                "product_id": item.product_id,
                "name": item.name,
                "proposed_profile": item.proposed_profile,
            }
            for item in preview
            if item.proposed_profile == "UNASSIGNED"
        ],
        "proposals": [item.__dict__ for item in preview],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
