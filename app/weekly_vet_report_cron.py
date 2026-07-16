from __future__ import annotations

import json
import os

from sqlalchemy import text

from .db import SessionLocal, init_db
from .production_report_service import send_weekly_vet_report_once


def run_if_due() -> dict[str, object]:
    """Send the report only during the configured Monday hour in Athens."""
    init_db()
    db = SessionLocal()
    try:
        target_hour = int(os.getenv("WEEKLY_VET_REPORT_HOUR", "8"))
        athens_now = db.execute(
            text("SELECT NOW() AT TIME ZONE 'Europe/Athens'")
        ).scalar()
        athens_hour = int(athens_now.hour)
        if athens_now.weekday() != 0 or athens_hour != target_hour:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not-monday-target-hour",
                "athens_time": athens_now.isoformat(),
                "target_hour": target_hour,
            }

        return send_weekly_vet_report_once(db)
    finally:
        db.close()


def main() -> None:
    print(json.dumps(run_if_due(), ensure_ascii=False))


if __name__ == "__main__":
    main()
