"""Tests for connection_watcher + handle_connection in freshness_sync.py.

TDD RED → GREEN (migrated from Redis BLPOP to DB polling).

All tests use a real SQLite DB (conftest ``db`` fixture) + mocked scraper.
No live PJUD, no Playwright, no Redis required.

Security invariant tested:
  INV-2 — decrypted password MUST NOT appear in any log during handle_connection.
"""

import asyncio
import logging
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pjud_session(session_id: str = "sess-abc", lawyer_id: int = 1) -> MagicMock:
    """Return a minimal PJUDSession-like mock."""
    from datetime import timedelta

    s = MagicMock()
    s.session_id = session_id
    s.lawyer_id = lawyer_id
    s.rut = "12345678-9"
    s.is_expired.return_value = False
    s.time_until_expiry.return_value = timedelta(minutes=90)
    return s


def _make_pjud_case(rol: str = "C-0001-2026") -> MagicMock:
    c = MagicMock()
    c.rol = rol
    c.tribunal = "Juzgado Civil de Santiago"
    c.caratulado = "TEST vs TEST"
    c.fecha_ingreso = "2026-01-01"
    c.estado_cuaderno = "activo"
    c.cuaderno = "Principal"
    c.institucion = None
    return c


def _make_lawyer_in_db(db, lawyer_id_hint: int = 42):
    """Create a lawyer with credentials in the SQLite test DB."""
    from app.models.lawyer import Lawyer
    from app.core.security import encrypt_pjud_password

    lawyer = Lawyer(
        rut="12345678-9",
        name="Test Lawyer",
        encrypted_pjud_password=encrypt_pjud_password("plain-password"),
    )
    db.add(lawyer)
    db.flush()
    return lawyer


# ---------------------------------------------------------------------------
# Test 1 — segunda_clave success path
# ---------------------------------------------------------------------------


class TestHandleConnectionSuccess:
    """segunda_clave happy path: DB status transitions to 'connected'."""

    @pytest.mark.asyncio
    async def test_segunda_clave_success_writes_connected_status(self, db):
        """handle_connection sets status='connected' with session_id + cases_synced."""
        from scripts.freshness_sync import handle_connection
        from app.services.connection_queue import enqueue_connection, get_status

        lawyer = _make_lawyer_in_db(db)
        cid = enqueue_connection(
            db, lawyer_id=lawyer.id, rut="12345678-9",
            auth_method="segunda_clave", captcha_token="tok",
        )
        lock = asyncio.Lock()
        cases = [_make_pjud_case(f"C-{i:04d}-2026") for i in range(5)]
        session = _make_pjud_session(session_id="sess-xyz", lawyer_id=lawyer.id)

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = {
            "connection_id": cid,
            "lawyer_id": lawyer.id,
            "rut": "12345678-9",
            "auth_method": "segunda_clave",
            "captcha_token": "tok",
        }

        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain-password"),
            patch("app.core.redis.get_async_redis_client", new_callable=AsyncMock, return_value=None),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(3, 2, []),
            ),
        ):
            await handle_connection(job, db, mock_sc, lock)

        status = get_status(db, cid)
        assert status is not None, "Status row must exist after handle_connection"
        assert status["status"] == "connected", f"Expected 'connected', got {status['status']!r}"
        assert status["session_id"] == "sess-xyz"
        assert status["cases_synced"] == 5  # len(api_cases)

    @pytest.mark.asyncio
    async def test_session_store_failure_does_not_crash_connect(self, db):
        """A Redis error during SessionStore.asave_session must NOT abort the flow."""
        from scripts.freshness_sync import handle_connection
        from app.services.connection_queue import enqueue_connection, get_status

        lawyer = _make_lawyer_in_db(db)
        cid = enqueue_connection(
            db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave", captcha_token="tok"
        )
        lock = asyncio.Lock()
        cases = [_make_pjud_case()]
        session = _make_pjud_session(session_id="sess-optional", lawyer_id=lawyer.id)

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = {
            "connection_id": cid,
            "lawyer_id": lawyer.id,
            "rut": "12345678-9",
            "auth_method": "segunda_clave",
            "captcha_token": "tok",
        }

        # Simulate Redis blowing up
        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain"),
            patch(
                "app.core.redis.get_async_redis_client",
                new_callable=AsyncMock,
                side_effect=Exception("Redis connection refused"),
            ),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(1, 0, []),
            ),
        ):
            await handle_connection(job, db, mock_sc, lock)

        status = get_status(db, cid)
        # Must still reach 'connected' — Redis failure is non-fatal
        assert status["status"] == "connected"


# ---------------------------------------------------------------------------
# Test 2 — login failure → status: failed
# ---------------------------------------------------------------------------


class TestHandleConnectionLoginFailure:
    """When login raises, DB status must be written as 'failed'."""

    @pytest.mark.asyncio
    async def test_login_raises_writes_failed_status(self, db):
        """A login exception produces a 'failed' status with an error message."""
        from scripts.freshness_sync import handle_connection
        from app.services.connection_queue import enqueue_connection, get_status

        lawyer = _make_lawyer_in_db(db)
        cid = enqueue_connection(
            db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave", captcha_token="tok"
        )
        lock = asyncio.Lock()

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(side_effect=Exception("token_expired"))

        job = {
            "connection_id": cid,
            "lawyer_id": lawyer.id,
            "rut": "12345678-9",
            "auth_method": "segunda_clave",
            "captcha_token": "tok",
        }

        with patch("app.core.security.decrypt_pjud_password", return_value="plain-pass"):
            await handle_connection(job, db, mock_sc, lock)

        status = get_status(db, cid)
        assert status is not None
        assert status["status"] == "failed"
        assert status["error"], "error field must be non-empty"


# ---------------------------------------------------------------------------
# Test 3 — INV-2: decrypted password must not appear in logs
# ---------------------------------------------------------------------------


class TestPasswordNotLeakedToLogs:
    """INV-2: the plaintext password MUST NOT appear in any log output."""

    @pytest.mark.asyncio
    async def test_decrypted_password_not_in_caplog(self, db, caplog):
        """caplog must not contain the plaintext password at any log level."""
        from scripts.freshness_sync import handle_connection
        from app.services.connection_queue import enqueue_connection

        PLAINTEXT_PASSWORD = "super-secret-pjud-pass-12345"

        lawyer = _make_lawyer_in_db(db)
        cid = enqueue_connection(
            db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave", captcha_token="tok"
        )
        lock = asyncio.Lock()
        cases = [_make_pjud_case()]
        session = _make_pjud_session(session_id="sess-inv2", lawyer_id=lawyer.id)

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = {
            "connection_id": cid,
            "lawyer_id": lawyer.id,
            "rut": "12345678-9",
            "auth_method": "segunda_clave",
            "captcha_token": "tok",
        }

        with caplog.at_level(logging.DEBUG):
            with (
                patch("app.core.security.decrypt_pjud_password", return_value=PLAINTEXT_PASSWORD),
                patch("app.core.redis.get_async_redis_client", new_callable=AsyncMock, return_value=None),
                patch("app.services.sync_service.SyncService.sync_cases"),
                patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
                patch(
                    "app.services.sync_service.detect_and_sync_movements",
                    new_callable=AsyncMock,
                    return_value=(0, 0, []),
                ),
            ):
                await handle_connection(job, db, mock_sc, lock)

        assert PLAINTEXT_PASSWORD not in caplog.text, (
            "Plaintext password found in log output — INV-2 violation!"
        )


# ---------------------------------------------------------------------------
# Test 4 — connection_watcher dispatches via asyncio.create_task
# ---------------------------------------------------------------------------


class TestConnectionWatcherDispatch:
    """Watcher must dispatch each job as an asyncio.Task, not await it inline."""

    @pytest.mark.asyncio
    async def test_watcher_dispatches_job_as_task(self):
        """connection_watcher calls asyncio.create_task for a pending job."""
        from scripts.freshness_sync import connection_watcher

        lock = asyncio.Lock()
        mock_sc = MagicMock()
        pending = {"count": 0}

        job = {
            "connection_id": "cid-watcher-1",
            "lawyer_id": 11,
            "rut": "1-1",
            "auth_method": "segunda_clave",
            "captcha_token": "tok",
        }

        call_count = {"n": 0}

        def mock_dequeue(db_session):
            call_count["n"] += 1
            return job if call_count["n"] == 1 else None

        mock_db = MagicMock()

        tasks_created = []
        original_create_task = asyncio.create_task

        def tracking_create_task(coro, **kwargs):
            t = original_create_task(coro, **kwargs)
            tasks_created.append(t)
            return t

        with (
            patch("app.core.database.SessionLocal", return_value=mock_db),
            patch("app.services.connection_queue.dequeue_connection", side_effect=mock_dequeue),
            patch("scripts.freshness_sync.CONNECTION_POLL_INTERVAL", 0),
            patch("asyncio.create_task", side_effect=tracking_create_task),
        ):
            watcher_task = asyncio.create_task(
                connection_watcher(mock_sc, lock, pending)
            )
            await asyncio.sleep(0.15)
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

        assert len(tasks_created) >= 1, (
            "connection_watcher must dispatch handle_connection via asyncio.create_task"
        )

        # Cleanup dispatched tasks
        for t in tasks_created:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


# ---------------------------------------------------------------------------
# Test 5 — lock serializes concurrent browser sections
# ---------------------------------------------------------------------------


class TestLockSerialization:
    """pjud_browser_lock must prevent two concurrent browser operations."""

    @pytest.mark.asyncio
    async def test_two_concurrent_handle_connections_are_serialized(self, db):
        """Two concurrent handle_connection calls must not overlap inside the lock."""
        from scripts.freshness_sync import handle_connection
        from app.services.connection_queue import enqueue_connection

        lawyer = _make_lawyer_in_db(db)
        lock = asyncio.Lock()
        concurrency = {"current": 0, "max_seen": 0}

        async def slow_login(rut, password, captcha_token):
            concurrency["current"] += 1
            concurrency["max_seen"] = max(concurrency["max_seen"], concurrency["current"])
            await asyncio.sleep(0.05)
            concurrency["current"] -= 1
            return _make_pjud_session(session_id="sess-lock", lawyer_id=lawyer.id)

        cases = [_make_pjud_case()]
        mock_sc = MagicMock()
        mock_sc.login_with_token = slow_login
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        cid1 = enqueue_connection(db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave", captcha_token="t1")
        cid2 = enqueue_connection(db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave", captcha_token="t2")

        job1 = {"connection_id": cid1, "lawyer_id": lawyer.id, "rut": "12345678-9", "auth_method": "segunda_clave", "captcha_token": "t1"}
        job2 = {"connection_id": cid2, "lawyer_id": lawyer.id, "rut": "12345678-9", "auth_method": "segunda_clave", "captcha_token": "t2"}

        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain"),
            patch("app.core.redis.get_async_redis_client", new_callable=AsyncMock, return_value=None),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=[]),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(0, 0, []),
            ),
        ):
            await asyncio.gather(
                handle_connection(job1, db, mock_sc, lock),
                handle_connection(job2, db, mock_sc, lock),
            )

        assert concurrency["max_seen"] == 1, (
            f"Expected max 1 concurrent browser section, got {concurrency['max_seen']}."
        )
