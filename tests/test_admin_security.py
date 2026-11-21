import time

from app.auth import _clear_login_failures, _record_login_failure
from app.kiosk_session import issue_kiosk_device_token, read_kiosk_device_uid, rotate_kiosk_key
from app.request_security import CSP
from app.db import Base
from app.models import AppFlag, User
from sqlalchemy.orm import sessionmaker
from tests.db_test_support import create_characterization_engine


def test_admin_pin_locks_for_fifteen_minutes_after_five_failures() -> None:
    _clear_login_failures("lock-admin")
    retry = 0
    for _ in range(5):
        retry = _record_login_failure("lock-admin", window=15 * 60, maximum=5)
    assert retry >= 15 * 60 - 2


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
