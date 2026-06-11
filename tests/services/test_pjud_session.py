"""S1-T1: Tests for the canonical PJUDSession value object."""
import json
import pytest
from datetime import datetime, timedelta, timezone


class TestPJUDSessionCreate:
    """PJUDSession.create() factory tests (S1-T1)."""

    def test_create_sets_utc_aware_created_at(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s.created_at.tzinfo is not None

    def test_create_sets_utc_aware_expires_at(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s.expires_at.tzinfo is not None

    def test_create_expires_at_is_25_min_after_created_at(self):
        from app.services.pjud_session import PJUDSession, SESSION_EXPIRY_MINUTES
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        delta = s.expires_at - s.created_at
        assert abs(delta.total_seconds() - SESSION_EXPIRY_MINUTES * 60) < 1

    def test_create_generates_unique_session_ids(self):
        from app.services.pjud_session import PJUDSession
        s1 = PJUDSession.create(rut="12345678-9", cookies=[])
        s2 = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s1.session_id != s2.session_id

    def test_create_defaults_auth_method_captcha(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s.auth_method == "captcha"

    def test_create_accepts_clave_unica_auth_method(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[], auth_method="clave_unica")
        assert s.auth_method == "clave_unica"

    def test_create_defaults_lawyer_id_zero(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s.lawyer_id == 0

    def test_create_accepts_lawyer_id(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[], lawyer_id=42)
        assert s.lawyer_id == 42


class TestPJUDSessionIsExpired:
    """is_expired() compares UTC-aware datetimes (S1-T1)."""

    def test_not_expired_when_fresh(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        assert s.is_expired() is False

    def test_expired_when_expires_at_in_past(self):
        from app.services.pjud_session import PJUDSession
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        s.expires_at = past
        assert s.is_expired() is True

    def test_aware_vs_aware_no_typeerror(self):
        """Comparing aware datetimes must not raise TypeError."""
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        result = s.is_expired()
        assert isinstance(result, bool)


class TestPJUDSessionRedisRoundTrip:
    """to_redis / from_redis round-trip (S1-T1)."""

    def test_round_trip_preserves_session_id(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[{"name": "c", "value": "v"}])
        assert PJUDSession.from_redis(s.to_redis()).session_id == s.session_id

    def test_round_trip_preserves_lawyer_id(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[], lawyer_id=42)
        assert PJUDSession.from_redis(s.to_redis()).lawyer_id == 42

    def test_round_trip_preserves_aware_created_at(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        restored = PJUDSession.from_redis(s.to_redis())
        assert restored.created_at.tzinfo is not None

    def test_round_trip_preserves_aware_expires_at(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        restored = PJUDSession.from_redis(s.to_redis())
        assert restored.expires_at.tzinfo is not None

    def test_round_trip_naive_timestamps_get_utc(self):
        """Naive ISO strings in Redis get UTC tzinfo on read."""
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[])
        raw_dict = json.loads(s.to_redis())
        raw_dict["created_at"] = s.created_at.replace(tzinfo=None).isoformat()
        raw_dict["expires_at"] = s.expires_at.replace(tzinfo=None).isoformat()
        restored = PJUDSession.from_redis(json.dumps(raw_dict))
        assert restored.created_at.tzinfo is not None
        assert restored.expires_at.tzinfo is not None

    def test_round_trip_cookies_preserved(self):
        from app.services.pjud_session import PJUDSession
        cookies = [{"name": "PHPSESSID", "value": "abc123"}]
        s = PJUDSession.create(rut="12345678-9", cookies=cookies)
        assert PJUDSession.from_redis(s.to_redis()).cookies == cookies

    def test_round_trip_auth_method_preserved(self):
        from app.services.pjud_session import PJUDSession
        s = PJUDSession.create(rut="12345678-9", cookies=[], auth_method="clave_unica")
        assert PJUDSession.from_redis(s.to_redis()).auth_method == "clave_unica"
