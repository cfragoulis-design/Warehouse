import time

from datetime import datetime, timedelta, timezone

from app import login_throttle
from app.kiosk_session import issue_kiosk_device_token, read_kiosk_device_uid, rotate_kiosk_key
from app.request_security import CSP
from app.db import Base
from app.models import AppFlag, User
from sqlalchemy.orm import sessionmaker
from tests.db_test_support import create_characterization_engine


def test_admin_pin_locks_for_fifteen_minutes_after_five_failures() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        limits = login_throttle.limits_for_role("admin")
        now = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
        retry = 0
        for i in range(5):
            retry = login_throttle.record_failure(db, "lock-admin", "ip:203.0.113.5", limits, now=now + timedelta(seconds=i))
        assert retry >= 15 * 60 - 6
        assert login_throttle.retry_after(db, "lock-admin", "device:4", limits, now=now) == 0
        later = now + timedelta(minutes=16)
        assert login_throttle.retry_after(db, "lock-admin", "ip:203.0.113.5", limits, now=later) == 0
        login_throttle.record_success(db, "lock-admin", "ip:203.0.113.5", now=later)
        assert login_throttle.retry_after(db, "lock-admin", "ip:203.0.113.5", limits, now=later) == 0
    finally:
        db.close()


def test_rotated_kiosk_key_rejects_the_previous_token() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        old = issue_kiosk_device_token(4, 1)
        assert read_kiosk_device_uid(old, key_version=1) == 4
        version = rotate_kiosk_key(db)
        assert version == 2
        assert read_kiosk_device_uid(old, key_version=version) is None
        fresh = issue_kiosk_device_token(4, version)
        assert read_kiosk_device_uid(fresh, key_version=version) == 4
        assert db.get(AppFlag, "kiosk_key_version").note == "2"
    finally:
        db.close()


def test_csp_blocks_framing_and_objects() -> None:
    assert "frame-ancestors 'none'" in CSP
    assert "object-src 'none'" in CSP
    assert "default-src 'self'" in CSP
    assert time.time() > 0
    assert User.__tablename__ == "users"
