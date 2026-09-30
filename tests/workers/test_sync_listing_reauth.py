"""Re-auth recovery in the LISTING phase of sync_lawyer_cases.

A session can exist in Redis while PJUD already invalidated it server-side.
``scraper.get_my_cases`` then raises ``SessionNotAuthenticatedError``. The
worker must invalidate the cached session, re-authenticate and retry the
listing exactly once. A ``ShapeChallengeError`` (a subclass) is a PJUD block,
not a credential problem, and must NEVER trigger a re-login.

All collaborators are mocked — no live connections.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.sync_history import SyncHistory
from app.scrapper.pjud.exceptions import (
    SessionNotAuthenticatedError,
    ShapeChallengeError,
)


def _not_authenticated() -> SessionNotAuthenticatedError:
    return SessionNotAuthenticatedError(
        url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        jquery_present=True,
        looks_like_login=True,
    )


def _shape_challenge() -> ShapeChallengeError:
    return ShapeChallengeError(
        url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        jquery_present=False,
        looks_like_login=False,
        marker="TSPD",
    )


class _Harness:
    """Patches every collaborator of sync_lawyer_cases and exposes the mocks."""

    def __init__(self, *, redis_session, get_my_cases_effects, reauth_result):
        self.old_session = redis_session
        self.db = MagicMock()
        self.added: list = []
        self.db.add.side_effect = lambda r: self.added.append(r)

        self.store = MagicMock()
        self.store.get_session_by_lawyer = AsyncMock(return_value=redis_session)
        self.store.adelete_session = AsyncMock(return_value=True)

        self.scraper = MagicMock()
        self.scraper.get_my_cases = AsyncMock(side_effect=get_my_cases_effects)
        self.scraper.close = AsyncMock()

        self.reauth = AsyncMock(return_value=reauth_result)
        self.detect = AsyncMock(return_value=(0, 0, []))

    async def run(self) -> dict:
        from app.workers.sync_scheduler import sync_lawyer_cases

        with patch("app.workers.sync_scheduler.get_session_store", return_value=self.store), \
             patch("app.api.v1.pjud.get_scraper", return_value=self.scraper), \
             patch("app.workers.sync_scheduler._reauth", self.reauth), \
             patch("app.workers.sync_scheduler.SyncService") as sync_service_cls, \
             patch(
                 "app.workers.sync_scheduler._select_cases_for_detail_rotation",
                 return_value=[],
             ), \
             patch("app.workers.sync_scheduler.detect_and_sync_movements", self.detect):
            sync_service_cls.return_value.sync_cases.return_value = MagicMock(
                cases_total=0, cases_new=0
            )
            return await sync_lawyer_cases(lawyer_id=1, competencia="civil", db=self.db)


class TestListingPhaseReauth:
    @pytest.mark.asyncio
    async def test_stale_redis_session_reauths_and_retries_once(self):
        """Invalid session -> invalidate, re-auth, retry with the NEW session."""
        old, new = MagicMock(name="old"), MagicMock(name="new")
        old.session_id, new.session_id = "old-id", "new-id"
        h = _Harness(
            redis_session=old,
            get_my_cases_effects=[_not_authenticated(), []],
            reauth_result=(new, None),
        )

        result = await h.run()

        assert result.get("success") is True
        assert h.scraper.get_my_cases.await_count == 2
        assert h.scraper.get_my_cases.await_args_list[0].kwargs["session"] is old
        assert h.scraper.get_my_cases.await_args_list[1].kwargs["session"] is new
        h.store.adelete_session.assert_awaited_once_with("old-id")
        h.reauth.assert_awaited_once()
        # The detail phase must continue with the fresh session, not the dead one.
        assert h.detect.await_args.kwargs["pjud_session"] is new

    @pytest.mark.asyncio
    async def test_retry_happens_only_once(self):
        """A second auth failure is NOT retried a third time; the run fails as before."""
        h = _Harness(
            redis_session=MagicMock(session_id="old-id"),
            get_my_cases_effects=[_not_authenticated(), _not_authenticated(), []],
            reauth_result=(MagicMock(session_id="new-id"), None),
        )

        result = await h.run()

        assert result.get("success") is False
        assert h.scraper.get_my_cases.await_count == 2
        h.reauth.assert_awaited_once()
        h.detect.assert_not_awaited()
        failed = [r for r in h.added if isinstance(r, SyncHistory)]
        assert len(failed) == 1 and failed[0].cases_found == 0

    @pytest.mark.asyncio
    async def test_shape_challenge_never_triggers_reauth(self):
        """Shape is a PJUD block, not a credential problem: no re-login, no retry."""
        h = _Harness(
            redis_session=MagicMock(session_id="old-id"),
            get_my_cases_effects=[_shape_challenge(), []],
            reauth_result=(MagicMock(session_id="new-id"), None),
        )

        result = await h.run()

        assert result.get("success") is False
        h.reauth.assert_not_awaited()
        h.store.adelete_session.assert_not_awaited()
        assert h.scraper.get_my_cases.await_count == 1
        h.detect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reauth_without_session_fails_without_retry(self):
        """If re-auth yields no session the run fails as today, with no retry."""
        h = _Harness(
            redis_session=MagicMock(session_id="old-id"),
            get_my_cases_effects=[_not_authenticated(), []],
            reauth_result=(None, "invalid_credentials"),
        )

        result = await h.run()

        assert result.get("success") is False
        h.reauth.assert_awaited_once()
        assert h.scraper.get_my_cases.await_count == 1
        h.detect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reauth_commits_lawyer_row_changes(self):
        """_reauth mutates the lawyer row (credential alert flag): it must be committed."""
        h = _Harness(
            redis_session=MagicMock(session_id="old-id"),
            get_my_cases_effects=[_not_authenticated(), []],
            reauth_result=(MagicMock(session_id="new-id"), None),
        )

        result = await h.run()

        assert result.get("success") is True
        assert h.db.commit.called

    @pytest.mark.asyncio
    async def test_missing_redis_session_path_is_unchanged(self):
        """Redis None -> re-auth up front, listing runs once, no invalidation."""
        new = MagicMock(name="new", session_id="new-id")
        h = _Harness(
            redis_session=None,
            get_my_cases_effects=[[]],
            reauth_result=(new, None),
        )

        result = await h.run()

        assert result.get("success") is True
        h.reauth.assert_awaited_once()
        assert h.scraper.get_my_cases.await_count == 1
        assert h.scraper.get_my_cases.await_args.kwargs["session"] is new
        h.store.adelete_session.assert_not_awaited()
        assert h.detect.await_args.kwargs["pjud_session"] is new


class TestPartialListing:
    """A listing cut short by PJUD keeps what was fetched and stays VISIBLE."""

    @staticmethod
    def _partial(n_cases=2):
        from app.scrapper.pjud.exceptions import PartialListingError

        cases = [
            MagicMock(rol=f"C-{i}-2024", tribunal="t", caratulado="a/b",
                      fecha_ingreso="01/01/2024", estado_cuaderno="Tramitación",
                      cuaderno="1 Principal", institucion="i")
            for i in range(n_cases)
        ]
        return PartialListingError(
            "list page 4: failed after 3 retries",
            cases=cases, failed_page=4, total_pages=139,
        ), cases

    @pytest.mark.asyncio
    async def test_partial_listing_syncs_the_cases_already_fetched(self):
        err, cases = self._partial()
        h = _Harness(redis_session=MagicMock(session_id="s"),
                     get_my_cases_effects=[err], reauth_result=(None, None))
        result = await h.run()
        assert result.get("success") is True
        # the detail phase receives the partial list, nothing was discarded
        assert h.detect.await_args.kwargs["api_cases"] == cases

    @pytest.mark.asyncio
    async def test_partial_listing_marks_run_partial_and_names_the_page(self):
        err, _ = self._partial()
        h = _Harness(redis_session=MagicMock(session_id="s"),
                     get_my_cases_effects=[err], reauth_result=(None, None))
        history = SyncHistory(lawyer_id=1, competencia="civil")
        h.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = history
        await h.run()
        assert history.status == "partial"
        assert history.error_message.startswith(
            "Listado incompleto: se cortó en la página 4 de 139. Se guardaron 2 causas."
        )

    @pytest.mark.asyncio
    async def test_listing_message_survives_a_detail_stop_reason(self):
        err, _ = self._partial()
        h = _Harness(redis_session=MagicMock(session_id="s"),
                     get_my_cases_effects=[err], reauth_result=(None, None))
        h.detect.return_value = (0, 0, ["Red o PJUD no disponible: x; lote detenido"])
        history = SyncHistory(lawyer_id=1, competencia="civil")
        h.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = history
        await h.run()
        assert history.status == "partial"
        assert "página 4 de 139" in history.error_message
        assert "lote detenido" in history.error_message

    @pytest.mark.asyncio
    async def test_happy_path_unchanged(self):
        h = _Harness(redis_session=MagicMock(session_id="s"),
                     get_my_cases_effects=[[]], reauth_result=(None, None))
        history = SyncHistory(lawyer_id=1, competencia="civil")
        history.status = "completed"
        h.db.query.return_value.filter.return_value.order_by.return_value.first.return_value = history
        result = await h.run()
        assert result["success"] is True
        assert history.status == "completed"
        assert history.error_message is None
