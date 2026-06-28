from __future__ import annotations

import json
import os

from sqlalchemy import text

from .db import SessionLocal, init_db
from .production_report_service import send_weekly_vet_report_once


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        target_hour = int(os.getenv("WEEKLY_VET_REPORT_HOUR", "8"))
        athens_hour = int(
            db.execute(text("SELECT EXTRACT(HOUR FROM NOW() AT TIME ZONE 'Europe/Athens')")).scalar()
        )
        if athens_hour != target_hour:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "outside-target-hour",
                        "athens_hour": athens_hour,
                        "target_hour": target_hour,
                    },
                    ensure_ascii=False,
                )
            )
            return

        result = send_weekly_vet_report_once(db)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
