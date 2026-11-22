from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.kiosk_health import record_heartbeat, should_alert, watch_silence
from app.models import KioskSilenceAlert
from tests.db_test_support import create_characterization_engine

ATHENS_NOON = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)  # 12:00 Athens in winter
NIGHT = datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc)  # 00:00 Athens


def test_silence_alerts_only_during_shop_hours() -> None:
    assert should_alert(None, None, ATHENS_NOON) is True
    assert should_alert(ATHENS_NOON - timedelta(minutes=5), None, ATHENS_NOON) is False
    assert should_alert(ATHENS_NOON - timedelta(minutes=20), None, ATHENS_NOON) is True
    assert should_alert(ATHENS_NOON - timedelta(minutes=20), ATHENS_NOON - timedelta(minutes=5), ATHENS_NOON) is False
    assert should_alert(None, None, NIGHT) is False


def test_watch_sends_once_until_the_gap_passes() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    sent = []
    try:
        record_heartbeat(db, {"mem_mb": 400, "disk_pct": 70, "temp_c": 55, "page_ok": True}, now=ATHENS_NOON - timedelta(minutes=30))
        assert watch_silence(db, now=ATHENS_NOON, send=lambda text: sent.append(text) or True) is True
        assert watch_silence(db, now=ATHENS_NOON + timedelta(minutes=1), send=lambda text: sent.append(text) or True) is False
        assert len(sent) == 1
        assert db.query(KioskSilenceAlert).count() == 1
    finally:
        db.close()
