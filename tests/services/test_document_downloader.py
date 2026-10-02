"""Tests for DocumentDownloader — failure isolation, throttle, DOC_DOWNLOAD_ENABLED gate.

Strategy: mock scraper.download_document_generic + StorageService.upload.
No network calls, no Playwright, no disk I/O.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fast_document_rate_limiter(monkeypatch):
    """Patch out the REAL process-global "document" token bucket.

    ``download_and_store`` acquires ``pjud_action_limiter("document")`` as a
    second, production-only throttle layer in addition to the injected
    ``limiter`` param. That bucket is a persistent module-global singleton
    shared by the whole pytest process (see
    ``app.scrapper.pjud.resilience.rate_limiter``), and its burst/rate are
    intentionally tiny for anti-Shape pacing (``PJUD_RL_DOCUMENT_BURST=1``,
    ``PJUD_RL_DOCUMENT_RATE=0.15`` — see app/config.py). Without this patch,
    any test downloading 2+ documents would incur real multi-second
    ``asyncio.sleep`` waits waiting for token refill, making these unit
    tests slow and order-dependent on unrelated tests' prior use of the
    same global bucket.
    """
    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
        MagicMock(return_value=fake_limiter),
    )

def _make_pending_doc(doc_id: int, endpoint="documentos/docuS.php", token="FAKE_TOKEN"):
    """Return a minimal Document-like mock with status='pending'."""
    doc = MagicMock()
    doc.id = doc_id
    doc.status = "pending"
    doc.pjud_endpoint = endpoint
    doc.pjud_token = token
    doc.doc_type = "resolution"
    doc.case_id = 1
    doc.pjud_token_hash = f"abc123def{doc_id:03d}"
    doc.failed_at = None
    return doc


def _make_scraper(side_effects):
    """Return a mock scraper whose download_document_generic yields side_effects in order."""
    scraper = MagicMock()
    scraper.download_document_generic = AsyncMock(side_effect=side_effects)
    return scraper


def _make_limiter():
    limiter = MagicMock()
    limiter.wait = AsyncMock()
    return limiter


def _make_storage_svc():
    svc = MagicMock()
    svc.upload = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestDocumentDownloaderFailureIsolation:
    @pytest.mark.asyncio
    async def test_second_doc_fails_others_still_stored(self):
        """Download failure on doc 2 → status=failed; docs 1 & 3 are stored."""
        import httpx

        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)
        doc3 = _make_pending_doc(3)

        scraper = _make_scraper([
            b"%PDF content1",
            httpx.RequestError("network error"),
            b"%PDF content3",
        ])
        db = MagicMock()
        storage_svc = _make_storage_svc()
        limiter = _make_limiter()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2, doc3],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=storage_svc,
            limiter=limiter,
            enabled=True,
        )

        # Docs 1 and 3 should have been uploaded
        storage_svc.upload.assert_any_call(doc1, b"%PDF content1")
        storage_svc.upload.assert_any_call(doc3, b"%PDF content3")
        # Doc 2 must be marked failed
        assert doc2.status == "failed"
        # Function must not have raised (we got here)

    @pytest.mark.asyncio
    async def test_no_exception_propagated_when_download_fails(self):
        """Even if all docs fail, download_and_store must not raise."""
        import httpx

        doc1 = _make_pending_doc(1)
        scraper = _make_scraper([httpx.RequestError("fail")])
        db = MagicMock()

        from app.services.document_downloader import DocumentDownloader
        # Must not raise:
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )
        assert doc1.status == "failed"

    @pytest.mark.asyncio
    async def test_download_exception_sets_failed_at(self):
        """FIX (timeline req #8): a failed download must stamp failed_at, not
        just flip status — the timeline needs a real transition timestamp."""
        import httpx

        doc1 = _make_pending_doc(1)
        assert doc1.failed_at is None
        scraper = _make_scraper([httpx.RequestError("network error")])
        db = MagicMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )
        assert doc1.status == "failed"
        assert doc1.failed_at is not None

    @pytest.mark.asyncio
    async def test_missing_endpoint_or_token_sets_failed_at(self):
        """FIX (timeline req #8): the missing-endpoint/token branch also
        transitions to 'failed' and must stamp failed_at the same way."""
        doc1 = _make_pending_doc(1, endpoint=None, token=None)
        assert doc1.failed_at is None
        db = MagicMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=_make_scraper([]),
            pjud_session=MagicMock(),
            db=db,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )
        assert doc1.status == "failed"
        assert doc1.failed_at is not None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestDocumentDownloaderRateLimiter:
    @pytest.mark.asyncio
    async def test_limiter_wait_called_for_each_doc(self):
        """limiter.wait() must be called once per pending document."""
        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)

        scraper = _make_scraper([b"%PDF", b"%PDF"])
        db = MagicMock()
        storage_svc = _make_storage_svc()
        limiter = _make_limiter()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=storage_svc,
            limiter=limiter,
            enabled=True,
        )

        assert limiter.wait.call_count == 2  # FIX 3: exactly once per pending doc


# ---------------------------------------------------------------------------
# DOC_DOWNLOAD_ENABLED gate
# ---------------------------------------------------------------------------

class TestDocumentDownloaderEnabledGate:
    @pytest.mark.asyncio
    async def test_enabled_false_skips_all_downloads(self):
        """When enabled=False, scraper.download_document_generic must not be called."""
        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)
        scraper = _make_scraper([])  # empty side_effects; any call would raise StopIteration

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=False,
        )

        scraper.download_document_generic.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_docs_are_skipped(self):
        """Docs with status='unavailable' must not be downloaded."""
        doc1 = _make_pending_doc(1)
        doc1.status = "unavailable"
        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        scraper.download_document_generic.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_stored_docs_are_skipped(self):
        """Docs with status='stored' must not be re-downloaded."""
        doc1 = _make_pending_doc(1)
        doc1.status = "stored"
        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        scraper.download_document_generic.assert_not_called()


# ---------------------------------------------------------------------------
# Retry failed docs (FIX 6)
# ---------------------------------------------------------------------------

class TestDocumentDownloaderRetryFailed:
    @pytest.mark.asyncio
    async def test_failed_doc_is_retried_on_next_call(self):
        """A doc with status='failed' must be retried and can become stored."""
        doc = _make_pending_doc(1)
        doc.status = "failed"  # simulate a previous failed attempt

        scraper = _make_scraper([b"%PDF retry content"])
        db = MagicMock()
        storage_svc = _make_storage_svc()
        limiter = _make_limiter()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=storage_svc,
            limiter=limiter,
            enabled=True,
        )

        storage_svc.upload.assert_called_once_with(doc, b"%PDF retry content")

    @pytest.mark.asyncio
    async def test_unavailable_doc_is_never_retried(self):
        """A doc with status='unavailable' must NOT be retried (terminal state)."""
        doc = _make_pending_doc(1)
        doc.status = "unavailable"
        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        scraper.download_document_generic.assert_not_called()


# ---------------------------------------------------------------------------
# AsyncSleepLimiter always yields (FIX 7 + FIX 8 rename)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Session error propagation (FIX 5)
# ---------------------------------------------------------------------------

class TestDocumentDownloaderSessionErrorPropagation:
    @pytest.mark.asyncio
    async def test_session_expired_error_propagates_does_not_mark_failed(self):
        """SessionExpiredError during download must propagate, not mark docs failed.

        Swallowing a session expiry would burn O(N) scraper calls against a dead
        session and mark all remaining docs failed for a transient auth problem.
        The caller (detect_and_sync_movements) owns reauth — let it propagate.
        """
        from app.scrapper.pjud.exceptions import SessionExpiredError
        from app.services.document_downloader import DocumentDownloader

        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)

        scraper = _make_scraper([SessionExpiredError("session dead")])
        db = MagicMock()

        with pytest.raises(SessionExpiredError):
            await DocumentDownloader().download_and_store(
                pending_docs=[doc1, doc2],
                scraper=scraper,
                pjud_session=MagicMock(),
                db=db,
                storage_service=_make_storage_svc(),
                limiter=_make_limiter(),
                enabled=True,
            )

        # doc1 must NOT be marked failed — it was a session error, not a doc error
        assert doc1.status == "pending", (
            "SessionExpiredError must not mark the document as failed"
        )
        # doc2 must not have been attempted (scraper raises on first call)
        assert scraper.download_document_generic.await_count == 1


class TestAsyncSleepLimiter:
    @pytest.mark.asyncio
    async def test_limiter_always_awaits_even_with_zero_delay(self):
        """AsyncSleepLimiter must call asyncio.sleep even when delay=0."""
        from unittest.mock import AsyncMock, patch
        from app.services.document_downloader import AsyncSleepLimiter

        limiter = AsyncSleepLimiter(delay=0.0)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.wait()

        mock_sleep.assert_called_once_with(0.0)

    @pytest.mark.asyncio
    async def test_limiter_awaits_with_positive_delay(self):
        """AsyncSleepLimiter must call asyncio.sleep with the configured delay."""
        from unittest.mock import AsyncMock, patch
        from app.services.document_downloader import AsyncSleepLimiter

        limiter = AsyncSleepLimiter(delay=0.5)
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.wait()

        mock_sleep.assert_called_once_with(0.5)


# ---------------------------------------------------------------------------
# Observability: the ORIGINAL exception must always reach the log
# ---------------------------------------------------------------------------

class _PoisonableSession:
    """Fake SQLAlchemy session reproducing the poisoned-session cascade.

    The first ``commit()`` fails with a recognisable proxy-cut error, which
    leaves the session needing a ``rollback()``. Until that rollback happens,
    every further ``commit()`` raises ``PendingRollbackError`` — exactly what
    a real Session does after a failed flush.
    """

    def __init__(self, original_exc: Exception, fail_first_n_commits: int = 1):
        from sqlalchemy.exc import PendingRollbackError

        self._original_exc = original_exc
        self._remaining_failures = fail_first_n_commits
        self._pending_rollback_error = PendingRollbackError
        self.needs_rollback = False
        self.commits_ok = 0
        self.rollbacks = 0

    @property
    def is_poisoned(self) -> bool:
        return self.needs_rollback

    def commit(self):
        if self.needs_rollback:
            raise self._pending_rollback_error(
                "This Session's transaction has been rolled back due to a "
                "previous exception during flush."
            )
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            self.needs_rollback = True
            raise self._original_exc
        self.commits_ok += 1

    def rollback(self):
        self.rollbacks += 1
        self.needs_rollback = False


class _ExpiredAttrDoc:
    """Document-like object whose ``id`` needs a DB refresh (expired attribute).

    After a successful commit SQLAlchemy expires loaded attributes; reading
    one while the session is poisoned raises ``PendingRollbackError``.
    """

    def __init__(self, session: _PoisonableSession, doc_id: int):
        self._session = session
        self._id = doc_id
        self.status = "pending"
        self.pjud_endpoint = "documentos/newebookcivil.php"
        self.pjud_token = "FAKE_TOKEN"
        self.doc_type = "ebook"
        self.failed_at = None
        self.gcs_path = None
        self.texto = None
        self.text_extracted_at = None

    @property
    def id(self):
        if self._session.is_poisoned:
            from sqlalchemy.exc import PendingRollbackError

            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a "
                "previous exception during flush."
            )
        return self._id


def _proxy_cut_error():
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "INSERT INTO documents ...",
        {},
        Exception("server closed the connection unexpectedly"),
    )


class TestDocumentDownloaderLogsOriginalException:
    @pytest.mark.asyncio
    async def test_commit_failure_logs_original_error_not_just_the_cascade(
        self, caplog
    ):
        """The proxy cut during commit must be visible in the log by type and
        message, not hidden behind PendingRollbackError."""
        from app.services.document_downloader import DocumentDownloader

        session = _PoisonableSession(_proxy_cut_error())
        doc = _ExpiredAttrDoc(session, 7)

        with caplog.at_level("DEBUG", logger="app.services.document_downloader"):
            await DocumentDownloader().download_and_store(
                pending_docs=[doc],
                scraper=_make_scraper([b"%PDF-1.4 fake"]),
                pjud_session=MagicMock(),
                db=session,
                storage_service=_make_storage_svc(),
                limiter=_make_limiter(),
                enabled=True,
            )

        logged = "\n".join(
            r.getMessage() + (r.exc_text or "") + _format_exc_info(r)
            for r in caplog.records
        )
        assert "OperationalError" in logged
        assert "server closed the connection unexpectedly" in logged

    @pytest.mark.asyncio
    async def test_original_exception_is_attached_with_traceback(self, caplog):
        from app.services.document_downloader import DocumentDownloader

        session = _PoisonableSession(_proxy_cut_error())
        doc = _ExpiredAttrDoc(session, 7)

        with caplog.at_level("DEBUG", logger="app.services.document_downloader"):
            await DocumentDownloader().download_and_store(
                pending_docs=[doc],
                scraper=_make_scraper([b"%PDF-1.4 fake"]),
                pjud_session=MagicMock(),
                db=session,
                storage_service=_make_storage_svc(),
                limiter=_make_limiter(),
                enabled=True,
            )

        with_exc = [r for r in caplog.records if r.exc_info]
        assert any(
            r.exc_info[0].__name__ == "OperationalError" for r in with_exc
        ), "no log record carries the original exception's exc_info"

    @pytest.mark.asyncio
    async def test_failure_state_is_written_even_when_session_is_poisoned(self):
        """The handler must roll back first so marking the doc as failed does
        not blow up with PendingRollbackError and mask the original error."""
        from app.services.document_downloader import DocumentDownloader

        session = _PoisonableSession(_proxy_cut_error())
        doc = _ExpiredAttrDoc(session, 7)

        await DocumentDownloader().download_and_store(
            pending_docs=[doc],
            scraper=_make_scraper([b"%PDF-1.4 fake"]),
            pjud_session=MagicMock(),
            db=session,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        assert session.rollbacks >= 1
        assert doc.status == "failed"
        assert doc.failed_at is not None
        assert session.commits_ok == 1  # the failure-state commit succeeded

    @pytest.mark.asyncio
    async def test_poisoned_doc_does_not_stop_the_following_docs(self):
        from app.services.document_downloader import DocumentDownloader

        session = _PoisonableSession(_proxy_cut_error())
        doc1 = _ExpiredAttrDoc(session, 1)
        doc2 = _ExpiredAttrDoc(session, 2)

        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2],
            scraper=_make_scraper([b"%PDF-1.4 a", b"%PDF-1.4 b"]),
            pjud_session=MagicMock(),
            db=session,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        assert doc1.status == "failed"
        # doc2 went through the happy path: upload + successful commit.
        assert session.commits_ok == 2  # doc1 failure-state + doc2 stored

    @pytest.mark.asyncio
    async def test_unusable_session_still_leaves_original_in_log_and_continues(
        self, caplog
    ):
        """Even if the rollback itself fails (dead connection), the original
        error is logged, nothing propagates, and the next doc is processed."""
        from app.services.document_downloader import DocumentDownloader

        session = _PoisonableSession(_proxy_cut_error())

        def _broken_rollback():
            raise RuntimeError("connection is gone")

        session.rollback = _broken_rollback
        doc1 = _ExpiredAttrDoc(session, 1)
        scraper = _make_scraper([b"%PDF-1.4 a"])

        with caplog.at_level("DEBUG", logger="app.services.document_downloader"):
            await DocumentDownloader().download_and_store(
                pending_docs=[doc1],
                scraper=scraper,
                pjud_session=MagicMock(),
                db=session,
                storage_service=_make_storage_svc(),
                limiter=_make_limiter(),
                enabled=True,
            )

        logged = "\n".join(
            r.getMessage() + _format_exc_info(r) for r in caplog.records
        )
        assert "server closed the connection unexpectedly" in logged


def _format_exc_info(record) -> str:
    import traceback

    if not record.exc_info:
        return ""
    return "".join(traceback.format_exception(*record.exc_info))
