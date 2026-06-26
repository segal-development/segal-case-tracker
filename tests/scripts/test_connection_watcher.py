"""Tests for connection_watcher + handle_connection in freshness_sync.py.

TDD RED -> GREEN cycle (Task 3.1 / 3.2).

All tests use FakeRedis + mocked scraper — no live PJUD, no Playwright.

Security invariant tested:
  INV-2 — decrypted password MUST NOT appear in any log statement during the
           handle_connection flow.
"""

import asyncio
import logging
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis

from app.services.connection_queue import enqueue_connection, get_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pjud_session(session_id: str = "sess-abc", lawyer_id: int = 1) -> MagicMock:
    """Return a minimal PJUDSession-like mock."""
    s = MagicMock()
    s.session_id = session_id
    s.lawyer_id = lawyer_id
    s.rut = "12345678-9"
    s.is_expired.return_value = False
    # time_until_expiry returns a timedelta for asave_session TTL computation
    from datetime import timedelta
    s.time_until_expiry.return_value = timedelta(minutes=90)
    return s


def _make_pjud_case(rol: str = "C-0001-2026") -> MagicMock:
    """Return a minimal PJUDCase-like mock (attributes accessed by _sync_after_login)."""
    c = MagicMock()
    c.rol = rol
    c.tribunal = "Juzgado Civil de Santiago"
    c.caratulado = "TEST vs TEST"
    c.fecha_ingreso = "2026-01-01"
    c.estado_cuaderno = "activo"
    c.cuaderno = "Principal"
    c.institucion = None
    return c


def _make_job(
    connection_id: str = "cid-001",
    lawyer_id: int = 42,
    rut: str = "12345678-9",
    auth_method: str = "segunda_clave",
    captcha_token: str = "tok-abc",
) -> dict:
    return {
        "connection_id": connection_id,
        "lawyer_id": lawyer_id,
        "rut": rut,
        "auth_method": auth_method,
        "captcha_token": captcha_token,
    }


def _make_db_with_lawyer(
    lawyer_id: int = 42,
    encrypted_pjud_password: str = "ENCRYPTED",
    rut: str = "12345678-9",
) -> MagicMock:
    """Return a mock DB session whose query returns a lawyer with the given attrs."""
    lawyer = MagicMock()
    lawyer.id = lawyer_id
    lawyer.rut = rut
    lawyer.encrypted_pjud_password = encrypted_pjud_password
    lawyer.encrypted_clave_unica_password = None

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = lawyer
    return mock_db


# ---------------------------------------------------------------------------
# Test 1 — segunda_clave success path
# ---------------------------------------------------------------------------


class TestHandleConnectionSuccess:
    """Segunda_clave happy path: session saved, status -> connected, cases_synced set."""

    @pytest.mark.asyncio
    async def test_segunda_clave_success_writes_connected_status(self):
        """handle_connection writes 'connected' status with session_id + cases_synced."""
        from scripts.freshness_sync import handle_connection

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()
        mock_db = _make_db_with_lawyer(lawyer_id=42, encrypted_pjud_password="ENC")

        session = _make_pjud_session(session_id="sess-xyz", lawyer_id=42)
        cases = [_make_pjud_case(f"C-{i:04d}-2026") for i in range(5)]

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = _make_job(connection_id="cid-success", lawyer_id=42, captcha_token="token123")

        # Pre-set the pending status so the key exists (mirrors real enqueue flow)
        from app.services.connection_queue import set_status as _set
        await _set(fake_redis, "cid-success", {"status": "pending", "updated_at": "t"})

        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain-password"),
            patch("app.services.session_store.SessionStore.asave_session", new_callable=AsyncMock, return_value=True),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(3, 2, []),
            ),
        ):
            await handle_connection(job, fake_redis, mock_db, mock_sc, lock)

        status = await get_status(fake_redis, "cid-success")
        assert status is not None, "Status key must exist after handle_connection"
        assert status["status"] == "connected", f"Expected 'connected', got {status['status']!r}"
        assert status["session_id"] == "sess-xyz"
        assert isinstance(status["cases_synced"], int)
        assert status["cases_synced"] == 5  # len(api_cases)

    @pytest.mark.asyncio
    async def test_asave_session_is_called_on_success(self):
        """asave_session must be called to persist the session after login."""
        from scripts.freshness_sync import handle_connection

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()
        mock_db = _make_db_with_lawyer(lawyer_id=7, encrypted_pjud_password="ENC")

        session = _make_pjud_session(session_id="sess-save", lawyer_id=7)
        cases = [_make_pjud_case()]

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = _make_job(connection_id="cid-save", lawyer_id=7)

        mock_asave = AsyncMock(return_value=True)

        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain-pw"),
            patch("app.services.session_store.SessionStore.asave_session", mock_asave),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(1, 0, []),
            ),
        ):
            await handle_connection(job, fake_redis, mock_db, mock_sc, lock)

        mock_asave.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2 — expired / invalid captcha token -> status: failed
# ---------------------------------------------------------------------------


class TestHandleConnectionLoginFailure:
    """When login raises, status must be written as 'failed' with a reason."""

    @pytest.mark.asyncio
    async def test_login_raises_writes_failed_status(self):
        """A login exception produces a 'failed' status with the exception message as reason."""
        from scripts.freshness_sync import handle_connection

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()
        mock_db = _make_db_with_lawyer(lawyer_id=99, encrypted_pjud_password="ENC")

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(side_effect=Exception("token_expired"))

        job = _make_job(connection_id="cid-fail", lawyer_id=99)

        with patch("app.core.security.decrypt_pjud_password", return_value="plain-pass"):
            await handle_connection(job, fake_redis, mock_db, mock_sc, lock)

        status = await get_status(fake_redis, "cid-fail")
        assert status is not None
        assert status["status"] == "failed", f"Expected 'failed', got {status['status']!r}"
        assert "reason" in status
        assert status["reason"], "reason must be non-empty"


# ---------------------------------------------------------------------------
# Test 3 — INV-2: decrypted password must not appear in logs
# ---------------------------------------------------------------------------


class TestPasswordNotLeakedToLogs:
    """INV-2: the plaintext password MUST NOT appear in any log output."""

    @pytest.mark.asyncio
    async def test_decrypted_password_not_in_caplog(self, caplog):
        """caplog must not contain the plaintext password at any log level."""
        from scripts.freshness_sync import handle_connection

        PLAINTEXT_PASSWORD = "super-secret-pjud-pass-12345"

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()
        mock_db = _make_db_with_lawyer(lawyer_id=5, encrypted_pjud_password="ENC")

        session = _make_pjud_session(session_id="sess-inv2", lawyer_id=5)
        cases = [_make_pjud_case()]

        mock_sc = MagicMock()
        mock_sc.login_with_token = AsyncMock(return_value=session)
        mock_sc.get_my_cases = AsyncMock(return_value=cases)

        job = _make_job(connection_id="cid-inv2", lawyer_id=5)

        with caplog.at_level(logging.DEBUG):
            with (
                patch("app.core.security.decrypt_pjud_password", return_value=PLAINTEXT_PASSWORD),
                patch("app.services.session_store.SessionStore.asave_session", new_callable=AsyncMock, return_value=True),
                patch("app.services.sync_service.SyncService.sync_cases"),
                patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=cases),
                patch(
                    "app.services.sync_service.detect_and_sync_movements",
                    new_callable=AsyncMock,
                    return_value=(0, 0, []),
                ),
            ):
                await handle_connection(job, fake_redis, mock_db, mock_sc, lock)

        assert PLAINTEXT_PASSWORD not in caplog.text, (
            f"Plaintext password was found in log output! "
            f"This is a security invariant (INV-2) violation."
        )


# ---------------------------------------------------------------------------
# Test 4 — connection_watcher dispatches via asyncio.create_task
# ---------------------------------------------------------------------------


class TestConnectionWatcherDispatch:
    """Watcher must dispatch each job as an asyncio.Task, not await it inline."""

    @pytest.mark.asyncio
    async def test_watcher_calls_create_task_not_await(self):
        """connection_watcher must call asyncio.create_task for each job, not await inline."""
        from scripts.freshness_sync import connection_watcher

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()
        mock_db = MagicMock()
        mock_sc = MagicMock()
        pending = {"count": 0}

        # Pre-seed one job so the watcher picks it up
        job = _make_job(connection_id="cid-dispatch", lawyer_id=11)
        import json
        await fake_redis.lpush("pjud:connect:queue", json.dumps(job))

        tasks_created = []
        original_create_task = asyncio.create_task

        def tracking_create_task(coro, **kwargs):
            t = original_create_task(coro, **kwargs)
            tasks_created.append(t)
            return t

        with patch("asyncio.create_task", side_effect=tracking_create_task):
            # Run watcher for a short window: it should pick up the seeded job,
            # dispatch a task, then we cancel it.
            watcher_task = asyncio.create_task(
                connection_watcher(fake_redis, mock_db, mock_sc, lock, pending)
            )
            # Give it enough time to BLPOP + create_task
            await asyncio.sleep(0.15)
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

        assert len(tasks_created) >= 1, (
            "connection_watcher must dispatch handle_connection via asyncio.create_task"
        )

        # Cleanup any dispatched tasks
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
    async def test_two_concurrent_handle_connections_are_serialized(self):
        """Concurrent handle_connection calls must not overlap inside the lock.

        We track 'current' concurrent executions inside the mocked login;
        the max must never exceed 1 when both share the same Lock.
        """
        from scripts.freshness_sync import handle_connection

        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        lock = asyncio.Lock()

        concurrency = {"current": 0, "max_seen": 0}

        async def slow_login(rut, password, captcha_token):
            concurrency["current"] += 1
            concurrency["max_seen"] = max(concurrency["max_seen"], concurrency["current"])
            await asyncio.sleep(0.05)  # simulate browser latency
            concurrency["current"] -= 1
            return _make_pjud_session(session_id="sess-lock", lawyer_id=1)

        mock_sc = MagicMock()
        mock_sc.login_with_token = slow_login
        mock_sc.get_my_cases = AsyncMock(return_value=[_make_pjud_case()])

        job1 = _make_job(connection_id="cid-lock-1", lawyer_id=1)
        job2 = _make_job(connection_id="cid-lock-2", lawyer_id=2)

        mock_db1 = _make_db_with_lawyer(lawyer_id=1, encrypted_pjud_password="ENC")
        mock_db2 = _make_db_with_lawyer(lawyer_id=2, encrypted_pjud_password="ENC")

        with (
            patch("app.core.security.decrypt_pjud_password", return_value="plain"),
            patch("app.services.session_store.SessionStore.asave_session", new_callable=AsyncMock, return_value=True),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation", return_value=[]),
            patch(
                "app.services.sync_service.detect_and_sync_movements",
                new_callable=AsyncMock,
                return_value=(0, 0, []),
            ),
        ):
            await asyncio.gather(
                handle_connection(job1, fake_redis, mock_db1, mock_sc, lock),
                handle_connection(job2, fake_redis, mock_db2, mock_sc, lock),
            )

        assert concurrency["max_seen"] == 1, (
            f"Expected max 1 concurrent browser section (serialized by lock), "
            f"got {concurrency['max_seen']}. The lock is not being acquired correctly."
        )
