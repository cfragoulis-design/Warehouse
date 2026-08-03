from __future__ import annotations

import os


# Test collection must never inherit or contact a live database implicitly.
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault(
    "SECRET_KEY",
    "warehouse-test-session-secret-is-at-least-32-characters",
)
