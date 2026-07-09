"""
Tests for chunked commits + commit-retry resilience in SyncService.sync_cases.

Context: for a lawyer with ~2085 cases, the old code issued exactly ONE
`db.commit()` at the end of `sync_cases`. Over the Mac's Cloud SQL Auth Proxy,
that single big commit is slow enough that the proxy drops the connection
mid-commit (`psycopg2.OperationalError: server closed the connection
unexpectedly`), failing the sync of the ENTIRE lawyer.

TDD: written BEFORE the fix — everything below must FAIL (RED) on the
single-commit implementation and PASS (GREEN) once `sync_cases`:
  1. Commits every `settings.SYNC_COMMIT_CHUNK_SIZE` processed cases instead
     of once at the end (bounding data loss on a mid-run drop to one chunk).
  2. Retries a commit that raises `sqlalchemy.exc.OperationalError` up to
     `settings.SYNC_COMMIT_MAX_RETRIES` attempts (rollback + retry), and does
     NOT retry any other exception type.
  3. Disables `expire_on_commit` for the duration of the sync run so the
     interim commits don't expire the cached Court objects
     (`self._court_cache`) or the pre-loaded `existing_by_rol` Case objects
     — otherwise chunk boundaries would silently reintroduce the N+1 query
     pattern fixed by `test_sync_case_upsert_n1.py`.
"""

import re
import pytest
from datetime import datetime
from contextlib import contextmanager
from unittest.mock import Mock

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all
from app.main import app as _app  # noqa: F401 — side-effect import

from app.config import settings
from app.core.database import Base
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court

from app.services.sync_service import SyncService, ScrapedCase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sqlite_db(sqlite_engine):
    Session = sessionmaker(bind=sqlite_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def lawyer(sqlite_db):
    db = sqlite_db
    lawyer = Lawyer(
        rut="12345678-9",
        name="Abogada Test",
        email="test@segal.cl",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.commit()
    return lawyer


def _scraped_case(**kwargs):
    defaults = dict(
        rol="C-1234-2025",
        tribunal="24º Juzgado Civil de Santiago",
        caratulado="BANCO ITAU/FERNANDEZ",
        fecha_ingreso="01/01/2025",
        estado_cuaderno="Tramitación",
        cuaderno="Principal",
    )
    defaults.update(kwargs)
    return ScrapedCase(**defaults)


def _batch(n, offset=0, tribunales=None):
    tribunales = tribunales or ["Tribunal A", "Tribunal B", "Tribunal C"]
    return [
        _scraped_case(
            rol=f"C-{offset + i}-2025",
            tribunal=tribunales[i % len(tribunales)],
        )
        for i in range(n)
    ]


@contextmanager
def count_selects(engine, tables):
    pattern = re.compile(
        r"\bFROM\s+(" + "|".join(re.escape(t) for t in tables) + r")\b",
        re.IGNORECASE,
    )
    counters = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        stripped = statement.strip()
        if stripped[:6].upper() == "SELECT" and pattern.search(stripped):
            counters["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counters
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


# ---------------------------------------------------------------------------
# Chunked commit count
# ---------------------------------------------------------------------------

class TestChunkedCommitCount:
    def test_commits_roughly_once_per_chunk(self, sqlite_db, lawyer, monkeypatch):
        """Syncing N cases with chunk size K performs one commit per chunk
        plus one final commit that finalizes the SyncHistory record
        (~ceil(N/K) + 1) instead of the old single final commit — and all
        cases are still upserted correctly (behavior unchanged vs. a
        single-commit baseline).
        """
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 10)

        sync = SyncService(sqlite_db)
        commit_spy = Mock(wraps=sqlite_db.commit)
        sqlite_db.commit = commit_spy

        result = sync.sync_cases(
            lawyer_id=lawyer.id, scraped_cases=_batch(50), competencia="civil"
        )

        assert result.cases_new == 50
        assert result.cases_updated == 0
        assert sqlite_db.query(Case).filter(Case.lawyer_id == lawyer.id).count() == 50

        # ceil(50/10) == 5 chunk commits + 1 final commit == 6.
        # Old behavior would be exactly 1.
        assert commit_spy.call_count == 6

    def test_single_small_batch_commits_chunk_plus_final(self, sqlite_db, lawyer, monkeypatch):
        """A batch smaller than the chunk size performs exactly 2 commits:
        one for its single chunk, one final commit for the SyncHistory
        completion (old behavior was exactly 1 commit total)."""
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 200)

        sync = SyncService(sqlite_db)
        commit_spy = Mock(wraps=sqlite_db.commit)
        sqlite_db.commit = commit_spy

        sync.sync_cases(lawyer_id=lawyer.id, scraped_cases=_batch(5), competencia="civil")

        assert commit_spy.call_count == 2


# ---------------------------------------------------------------------------
# Partial progress survives a mid-run drop
# ---------------------------------------------------------------------------

class TestPartialProgressSurvivesDrop:
    def test_first_chunk_committed_when_second_chunk_fails_after_retries(
        self, sqlite_db, lawyer, monkeypatch
    ):
        """A simulated OperationalError on the 2nd chunk's commit, with
        retries exhausted, must leave the 1st chunk's cases COMMITTED —
        not rolled back along with everything else."""
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 1)
        monkeypatch.setattr(settings, "SYNC_COMMIT_MAX_RETRIES", 2)

        sync = SyncService(sqlite_db)
        real_commit = sqlite_db.commit
        call_count = {"n": 0}

        def flaky_commit():
            call_count["n"] += 1
            # 1st commit (chunk 1, case #1): succeeds.
            if call_count["n"] == 1:
                return real_commit()
            # 2nd+ commit attempts (chunk 2, case #2, incl. retries): always fail.
            raise OperationalError("commit", {}, Exception("server closed the connection unexpectedly"))

        sqlite_db.commit = flaky_commit

        with pytest.raises(OperationalError):
            sync.sync_cases(
                lawyer_id=lawyer.id, scraped_cases=_batch(3), competencia="civil"
            )

        # Restore the real commit to inspect persisted state.
        sqlite_db.commit = real_commit
        sqlite_db.rollback()

        persisted = sqlite_db.query(Case).filter(Case.lawyer_id == lawyer.id).all()
        assert len(persisted) == 1
        assert persisted[0].rol == "C-0-2025"

    def test_retry_only_on_operational_error(self, sqlite_db, lawyer, monkeypatch):
        """A non-OperationalError raised by commit() must NOT be retried —
        it propagates immediately."""
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 1)
        monkeypatch.setattr(settings, "SYNC_COMMIT_MAX_RETRIES", 2)

        sync = SyncService(sqlite_db)
        call_count = {"n": 0}

        def bad_commit():
            call_count["n"] += 1
            raise ValueError("not a connectivity issue")

        sqlite_db.commit = bad_commit

        with pytest.raises(ValueError):
            sync.sync_cases(
                lawyer_id=lawyer.id, scraped_cases=_batch(2), competencia="civil"
            )

        # No retry attempted for a non-OperationalError — commit was called once.
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Retry success
# ---------------------------------------------------------------------------

class TestCommitRetrySucceeds:
    def test_operational_error_then_success_completes_sync(self, sqlite_db, lawyer, monkeypatch):
        """An OperationalError on a commit that then succeeds on retry ->
        the sync completes, and all cases are persisted."""
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 200)
        monkeypatch.setattr(settings, "SYNC_COMMIT_MAX_RETRIES", 2)

        sync = SyncService(sqlite_db)
        real_commit = sqlite_db.commit
        call_count = {"n": 0}

        def flaky_then_ok_commit():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OperationalError("commit", {}, Exception("server closed the connection unexpectedly"))
            return real_commit()

        sqlite_db.commit = flaky_then_ok_commit

        result = sync.sync_cases(
            lawyer_id=lawyer.id, scraped_cases=_batch(5), competencia="civil"
        )

        assert result.cases_new == 5
        # 1 failed + 1 successful retry for the single chunk, + 1 final commit.
        assert call_count["n"] == 3
        assert sqlite_db.query(Case).filter(Case.lawyer_id == lawyer.id).count() == 5


# ---------------------------------------------------------------------------
# Query-count across chunks — the key guard against reintroducing the N+1
# ---------------------------------------------------------------------------

class TestQueryCountAcrossChunks:
    def test_case_and_court_selects_stay_o1_across_multiple_chunk_commits(
        self, sqlite_db, lawyer, monkeypatch
    ):
        """Syncing 50 cases with SYNC_COMMIT_CHUNK_SIZE=10 (5 chunk commits)
        must NOT reintroduce reload SELECTs on cases/courts at chunk
        boundaries — this is the expire_on_commit guard working."""
        monkeypatch.setattr(settings, "SYNC_COMMIT_CHUNK_SIZE", 10)

        sync = SyncService(sqlite_db)
        commit_spy = Mock(wraps=sqlite_db.commit)
        sqlite_db.commit = commit_spy

        with count_selects(sqlite_db.get_bind(), ["cases", "courts"]) as counts:
            sync.sync_cases(lawyer_id=lawyer.id, scraped_cases=_batch(50), competencia="civil")

        assert commit_spy.call_count == 6  # 5 chunk commits + 1 final commit
        # One pre-load SELECT for cases + up to 3 cache-miss SELECTs for the
        # 3 distinct tribunales = a small constant, independent of N and of
        # the number of interim chunk commits.
        assert counts["count"] <= 4


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_sync_commit_chunk_size_default(self):
        assert settings.SYNC_COMMIT_CHUNK_SIZE == 200

    def test_sync_commit_max_retries_default(self):
        assert settings.SYNC_COMMIT_MAX_RETRIES == 2
