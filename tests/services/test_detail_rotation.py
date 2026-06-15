"""Tests for the case-detail rotation selection logic (sync-rotacion Slice 1).

S1-T1: TestSelectCasesForDetailRotation — pure unit tests using SQLite in-process DB.
S1-T4: TestMarkDetailCheckedAt — mark-checked + robustness tests (async, use mocked scraper).

These tests drive the implementation of:
  - _select_cases_for_detail_rotation (S1-T3)
  - detect_and_sync_movements changes: selected_cases param + mark-checked (S1-T5)
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_case(
    db,
    lawyer_id: int,
    competencia: str,
    rol: str,
    filed_at: datetime | None = None,
    last_detail_checked_at: datetime | None = None,
) -> Case:
    """Create Lawyer + Court + Case rows and return the Case.

    Reuses an existing Lawyer/Court if lawyer_id already matches a flushed row.
    """
    # Ensure a lawyer row exists for this ID.
    existing_lawyer = db.query(Lawyer).filter(Lawyer.id == lawyer_id).first()
    if not existing_lawyer:
        lawyer = Lawyer(rut=f"{lawyer_id:08d}-{lawyer_id % 10}", name=f"Lawyer {lawyer_id}", is_active=True)
        db.add(lawyer)
        db.flush()
        lawyer_id = lawyer.id

    # Ensure a court row exists (reuse any).
    court = db.query(Court).first()
    if not court:
        court = Court(code="TEST-COURT", name="Tribunal Civil Test", region="RM", type="civil")
        db.add(court)
        db.flush()

    case = Case(
        lawyer_id=lawyer_id,
        court_id=court.id,
        rol=rol,
        competencia=competencia,
        plaintiff="Alice",
        defendant="Bob",
        status="active",
        filed_at=filed_at,
        last_detail_checked_at=last_detail_checked_at,
    )
    db.add(case)
    db.flush()
    return case


def _make_api_case(rol: str, case_token: str | None = "token-abc") -> MagicMock:
    """Return a minimal MagicMock with .rol and .case_token."""
    m = MagicMock()
    m.rol = rol
    m.case_token = case_token
    return m


def _make_detail_empty() -> MagicMock:
    """Return a mock detail response with no movements/entities/documents."""
    detail = MagicMock()
    detail.case_documents = []
    detail.movements = []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    detail.case = MagicMock()
    detail.case.rol = "C-FAKE-ROL"
    return detail


# ===========================================================================
# S1-T1: TestSelectCasesForDetailRotation
# ===========================================================================

class TestSelectCasesForDetailRotation:
    """_select_cases_for_detail_rotation returns rotation-ordered PJUDCase objects."""

    def test_nulls_come_before_checked_cases(self, db):
        """NULL last_detail_checked_at cases appear before non-null cases."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer_id_seed = 1
        existing_lawyer = db.query(Lawyer).first()
        if not existing_lawyer:
            lawyer = Lawyer(rut="00000001-1", name="Lawyer 1", is_active=True)
            db.add(lawyer)
            db.flush()
            lawyer_id_seed = lawyer.id
        else:
            lawyer_id_seed = existing_lawyer.id

        past_ts = datetime(2025, 1, 1)

        c1 = _seed_case(db, lawyer_id_seed, "civil", "C-NULL-1", filed_at=datetime(2024, 6, 1), last_detail_checked_at=None)
        c2 = _seed_case(db, lawyer_id_seed, "civil", "C-NULL-2", filed_at=datetime(2024, 5, 1), last_detail_checked_at=None)
        c3 = _seed_case(db, lawyer_id_seed, "civil", "C-CHECKED-1", filed_at=datetime(2024, 4, 1), last_detail_checked_at=past_ts)
        db.commit()

        api_cases = [
            _make_api_case("C-NULL-1"),
            _make_api_case("C-NULL-2"),
            _make_api_case("C-CHECKED-1"),
        ]

        result = _select_cases_for_detail_rotation(db, lawyer_id_seed, "civil", api_cases, batch_size=10)
        rols = [ac.rol for ac in result]

        # Both nulls must appear before the checked one
        assert "C-CHECKED-1" not in rols[:2], "checked case appeared before null cases"
        assert "C-NULL-1" in rols
        assert "C-NULL-2" in rols
        assert "C-CHECKED-1" in rols

    def test_year_filter_excludes_pre_min_year_cases(self, db):
        """Cases whose ROL year < DETAIL_MIN_YEAR (2021) are excluded; >= is kept."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = db.query(Lawyer).first()
        if not lawyer:
            lawyer = Lawyer(rut="00000001-1", name="Lawyer 1", is_active=True)
            db.add(lawyer)
            db.flush()
        lid = lawyer.id

        _seed_case(db, lid, "civil", "C-100-2019", filed_at=datetime(2019, 6, 1), last_detail_checked_at=None)
        _seed_case(db, lid, "civil", "C-200-2024", filed_at=datetime(2024, 6, 1), last_detail_checked_at=None)

        api_cases = [_make_api_case("C-100-2019"), _make_api_case("C-200-2024")]
        result = _select_cases_for_detail_rotation(db, lid, "civil", api_cases, batch_size=10)

        rols = [c.rol for c in result]
        assert "C-200-2024" in rols, "2024 case must be included"
        assert "C-100-2019" not in rols, "pre-2021 case must be excluded"

    def test_secondary_sort_filed_at_desc_within_null_group(self, db):
        """Within NULL group, more recent filed_at comes first."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000002-2", name="Lawyer 2", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="TEST-COURT", name="Test Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        # older filed_at first in DB to test ordering
        c_old = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-OLD-1", competencia="civil",
                     filed_at=datetime(2022, 1, 1), last_detail_checked_at=None, status="active")
        c_new = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-NEW-1", competencia="civil",
                     filed_at=datetime(2024, 6, 1), last_detail_checked_at=None, status="active")
        db.add_all([c_old, c_new])
        db.commit()

        api_cases = [_make_api_case("C-OLD-1"), _make_api_case("C-NEW-1")]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=10)

        rols = [ac.rol for ac in result]
        assert rols.index("C-NEW-1") < rols.index("C-OLD-1"), "newer filed_at should come first within NULL group"

    def test_batch_capped_at_batch_size(self, db):
        """Exactly batch_size cases are returned when DB has more eligible cases."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000003-3", name="Lawyer 3", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="TEST-COURT", name="Test Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        rols = [f"C-CAP-{i}" for i in range(7)]
        for r in rols:
            c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=r, competencia="civil",
                     status="active", last_detail_checked_at=None)
            db.add(c)
        db.commit()

        api_cases = [_make_api_case(r) for r in rols]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=3)

        assert len(result) == 3

    def test_token_join_excludes_absent_rol(self, db):
        """A DB case whose rol is not present in api_cases is excluded from results."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000004-4", name="Lawyer 4", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="TEST-COURT", name="Test Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        c = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-ABSENT-1", competencia="civil",
                 status="active", last_detail_checked_at=None)
        db.add(c)
        db.commit()

        # api_cases does NOT include C-ABSENT-1
        api_cases = [_make_api_case("C-OTHER-99")]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=10)

        assert all(ac.rol != "C-ABSENT-1" for ac in result)

    def test_no_token_excludes_case(self, db):
        """A DB case whose matching api_case has case_token=None is excluded."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000005-5", name="Lawyer 5", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="TEST-COURT", name="Test Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        c = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-NOTOKEN-1", competencia="civil",
                 status="active", last_detail_checked_at=None)
        db.add(c)
        db.commit()

        api_cases = [_make_api_case("C-NOTOKEN-1", case_token=None)]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=10)

        assert len(result) == 0

    def test_empty_db_fallback_returns_api_cases_slice(self, db):
        """With no Case rows for the lawyer+competencia, returns token-bearing api_cases[:batch_size]."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000006-6", name="Lawyer 6", is_active=True)
        db.add(lawyer)
        db.flush()
        db.commit()

        api_cases = [_make_api_case(f"C-FALLBACK-{i}") for i in range(10)]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=4)

        assert len(result) == 4
        # Should be the first 4 from api_cases (all have tokens via _make_api_case default)
        for i, ac in enumerate(result):
            assert ac.rol == f"C-FALLBACK-{i}"

    def test_empty_db_fallback_excludes_tokenless_cases(self, db):
        """With no DB rows, the fallback skips api_cases that have no case_token."""
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000007-7", name="Lawyer 7", is_active=True)
        db.add(lawyer)
        db.flush()
        db.commit()

        # Mix tokenless and tokened cases
        api_cases = [
            _make_api_case("C-NO-TOKEN-0", case_token=None),
            _make_api_case("C-TOKEN-1"),
            _make_api_case("C-NO-TOKEN-2", case_token=None),
            _make_api_case("C-TOKEN-3"),
        ]
        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=10)

        rols = [ac.rol for ac in result]
        assert "C-NO-TOKEN-0" not in rols
        assert "C-NO-TOKEN-2" not in rols
        assert "C-TOKEN-1" in rols
        assert "C-TOKEN-3" in rols

    def test_ghost_cases_fall_back_to_live_api_cases(self, db):
        """DB has rows whose ROLs are absent from api_cases (ghost/archived) — falls back to live cases.

        Regression for starvation bug: ghost rows with NULL last_detail_checked_at
        permanently top the rotation order, blocking live cases from ever being selected.
        Fix: when DB batch returns rows but none match api_cases, fall back to live
        token-bearing api_cases so rotation is not stuck.
        """
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000008-8", name="Lawyer Ghost", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="GHOST-COURT", name="Ghost Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        # DB has two ghost rows (removed from PJUD) with NULL timestamps
        for i in range(2):
            db.add(Case(
                lawyer_id=lawyer.id, court_id=court.id,
                rol=f"C-GHOST-ARCHIVED-{i}", competencia="civil",
                status="inactive", last_detail_checked_at=None,
            ))
        db.commit()

        # api_cases only has live cases that are NOT in DB
        live_api_cases = [_make_api_case(f"C-LIVE-{i}") for i in range(3)]

        result = _select_cases_for_detail_rotation(db, lawyer.id, "civil", live_api_cases, batch_size=2)

        # Must NOT return empty — ghost rows must not block live cases
        assert len(result) > 0, "ghost-case starvation: live cases blocked by archived ghost rows"
        rols = [ac.rol for ac in result]
        # All returned cases are live (from api_cases), none are ghost
        for rol in rols:
            assert rol.startswith("C-LIVE-"), f"unexpected rol in result: {rol}"

    def test_rotation_advances_on_second_run(self, db):
        """Two-run rotation test: second run returns different (older-unchecked) cases.

        Proves the rotation actually advances: once a batch is marked checked,
        the next selection should pick DIFFERENT cases (those not yet checked).
        """
        from app.services.sync_service import _select_cases_for_detail_rotation

        lawyer = Lawyer(rut="00000009-9", name="Lawyer Rotate", is_active=True)
        db.add(lawyer)
        db.flush()

        court = db.query(Court).first()
        if not court:
            court = Court(code="ROT-COURT", name="Rotation Court", region="RM", type="civil")
            db.add(court)
            db.flush()

        # Seed 5 cases, all unchecked
        rols = [f"C-ROT-{i}" for i in range(5)]
        for r in rols:
            db.add(Case(
                lawyer_id=lawyer.id, court_id=court.id, rol=r,
                competencia="civil", status="active",
                filed_at=datetime(2024, 1, 1),
                last_detail_checked_at=None,
            ))
        db.commit()

        api_cases = [_make_api_case(r) for r in rols]

        # Run 1: select 2
        batch1 = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=2)
        assert len(batch1) == 2
        batch1_rols = {ac.rol for ac in batch1}

        # Simulate marking batch1 as checked
        now = datetime.utcnow()
        for ac in batch1:
            case = db.query(Case).filter(Case.rol == ac.rol, Case.lawyer_id == lawyer.id).first()
            case.last_detail_checked_at = now
        db.commit()

        # Run 2: select 2 — must be DIFFERENT cases (the unchecked ones)
        batch2 = _select_cases_for_detail_rotation(db, lawyer.id, "civil", api_cases, batch_size=2)
        assert len(batch2) == 2
        batch2_rols = {ac.rol for ac in batch2}

        # At least one case in batch2 must not have been in batch1
        assert not batch1_rols.issubset(batch2_rols) or not batch2_rols.issubset(batch1_rols), (
            "second rotation run returned the same cases as the first — rotation is not advancing"
        )
        # More precise: batch2 must come from the unchecked pool
        unchecked_rols = set(rols) - batch1_rols
        assert batch2_rols.issubset(unchecked_rols), (
            f"batch2 {batch2_rols} should be from unchecked pool {unchecked_rols}"
        )


# ===========================================================================
# S1-T4: TestMarkDetailCheckedAt
# ===========================================================================

class TestMarkDetailCheckedAt:
    """detect_and_sync_movements sets last_detail_checked_at after each fetch."""

    @pytest.mark.asyncio
    async def test_timestamp_advances_on_zero_movement_success(self, db):
        """last_detail_checked_at is set after a successful fetch with 0 movements."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer = Lawyer(rut="10000001-1", name="Lawyer Mark1", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="MARK-COURT", name="Mark Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-MARK-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-MARK-1", "token-mark-1")
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        before = datetime.utcnow()
        await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
        )
        after = datetime.utcnow()

        db.refresh(case)
        assert case.last_detail_checked_at is not None
        assert before <= case.last_detail_checked_at <= after

    @pytest.mark.asyncio
    async def test_timestamp_advances_on_case_specific_error(self, db):
        """A generic Exception (not session expiry) still advances last_detail_checked_at."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer = Lawyer(rut="10000002-2", name="Lawyer Mark2", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="MARK-COURT2", name="Mark Court 2", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-MARK-2", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-MARK-2", "token-mark-2")
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=ValueError("parse error"))

        before = datetime.utcnow()
        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
        )
        after = datetime.utcnow()

        db.refresh(case)
        assert case.last_detail_checked_at is not None, "case-specific error must still advance timestamp"
        assert before <= case.last_detail_checked_at <= after
        assert len(errors) == 1, "error should be captured, not raised"

    @pytest.mark.asyncio
    async def test_timestamp_not_set_when_db_case_not_found(self, db):
        """No update when the api_case rol is not found in DB (just a warning, no crash)."""
        from app.services.sync_service import detect_and_sync_movements

        api_case = _make_api_case("C-GHOST-1", "token-ghost")
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        # No DB rows created — function must not crash
        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=999,
            api_cases=[api_case],
            selected_cases=[api_case],
        )

        assert movements_new == 0
        assert not errors

    @pytest.mark.asyncio
    async def test_exception_mid_entity_sync_no_partial_rows_but_timestamp_advances(self, db):
        """FIX 1: rollback partial entity rows on exception; last_detail_checked_at still advances.

        When _sync_entities raises mid-way, any flushed (but not yet committed) entity
        rows must be rolled back.  The except block must: (1) rollback partial state,
        (2) set last_detail_checked_at, (3) commit only that single-column UPDATE.
        """
        from app.services.sync_service import detect_and_sync_movements
        from app.models.case_litigante import CaseLitigante

        lawyer = Lawyer(rut="10000005-5", name="Lawyer Partial", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="PARTIAL-COURT", name="Partial Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-PARTIAL-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-PARTIAL-1", "token-partial")

        # Scraper returns successfully but _sync_entities raises after flush
        detail = _make_detail_empty()
        from unittest.mock import patch
        from app.models.case_litigante import CaseLitigante

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=detail)

        with patch(
            "app.services.sync_service._sync_entities",
            side_effect=RuntimeError("simulated entity sync failure"),
        ):
            movements_new, alerts_created, errors = await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=MagicMock(),
                lawyer_id=lawyer.id,
                api_cases=[api_case],
                selected_cases=[api_case],
            )

        assert len(errors) == 1, "error must be captured"

        # timestamp must be advanced despite the error (rotation fairness)
        db.expire(case)
        db.refresh(case)
        assert case.last_detail_checked_at is not None, "timestamp must advance even after entity sync error"

        # No partial litigante/entity rows should have been committed (rollback cleaned them up)
        litigante_count = db.query(CaseLitigante).filter(CaseLitigante.case_id == case.id).count()
        assert litigante_count == 0, f"partial litigante rows were committed: {litigante_count} rows found"

    @pytest.mark.asyncio
    async def test_session_error_does_not_advance_timestamp__slice2(self, db):
        """Slice 2: SessionExpiredError does NOT advance last_detail_checked_at.

        Session failure is not the case's fault — the PJUD session must be
        refreshed first.  The rotation position is preserved so the case is
        retried with a fresh session on the next scheduled run.

        Contrast with test_timestamp_advances_on_case_specific_error, where a
        case-specific (non-session) exception DOES advance the timestamp.
        """
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="10000006-6", name="Lawyer SessionGap", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="GAP-COURT", name="Gap Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-GAP-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-GAP-1", "token-gap")
        mock_scraper = MagicMock()
        # Raise a real SessionExpiredError (not a generic Exception)
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=SessionExpiredError("session expired")
        )

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
            # reauth_callback=None → batch stops gracefully after session error
        )

        db.refresh(case)
        # Slice 2 behavior: session error must NOT advance the timestamp
        assert case.last_detail_checked_at is None, (
            "SessionExpiredError must NOT advance last_detail_checked_at "
            "(session failure is not the case's fault)"
        )
        assert len(errors) == 1, "error should be captured (batch stopped)"

    @pytest.mark.asyncio
    async def test_case_specific_error_does_not_starve_next_run(self, db):
        """Both cases get last_detail_checked_at advanced — one via error path, one via success."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer = Lawyer(rut="10000003-3", name="Lawyer Mark3", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="MARK-COURT3", name="Mark Court 3", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_fail = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-FAIL-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_ok = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-OK-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_fail, case_ok])
        db.commit()

        api_fail = _make_api_case("C-FAIL-1", "token-fail")
        api_ok = _make_api_case("C-OK-1", "token-ok")

        call_count = 0

        async def side_effect_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated parse failure")
            return _make_detail_empty()

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=side_effect_fn)

        before = datetime.utcnow()
        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_fail, api_ok],
            selected_cases=[api_fail, api_ok],
        )
        after = datetime.utcnow()

        db.refresh(case_fail)
        db.refresh(case_ok)

        assert case_fail.last_detail_checked_at is not None, "failing case must still advance (rotation)"
        assert case_ok.last_detail_checked_at is not None, "success case must advance"
        assert before <= case_fail.last_detail_checked_at <= after
        assert before <= case_ok.last_detail_checked_at <= after
        assert len(errors) == 1  # only the failing case


# ===========================================================================
# S2-T1: TestReauthMidBatch — Slice 2 re-auth and session-expiry handling
# ===========================================================================

class TestReauthMidBatch:
    """detect_and_sync_movements re-auth callback + SessionExpiredError handling.

    All tests use @pytest.mark.slice2 so they can be run in isolation during
    Slice 2 development and excluded when verifying Slice 1 regressions.
    """

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_reauth_succeeds_case_is_retried(self, db):
        """SessionExpiredError triggers reauth_callback; retry succeeds; timestamp advanced."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000001-1", name="Lawyer Reauth1", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="REAUTH-COURT1", name="Reauth Court 1", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-REAUTH-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-REAUTH-1", "token-reauth-1")

        # First call raises SessionExpiredError; second succeeds
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=[SessionExpiredError("expired"), _make_detail_empty()]
        )

        fresh_session = MagicMock()
        reauth_callback = AsyncMock(return_value=fresh_session)

        before = datetime.utcnow()
        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
            reauth_callback=reauth_callback,
        )
        after = datetime.utcnow()

        # Scraper called twice: initial attempt + retry after reauth
        assert mock_scraper.get_case_detail.await_count == 2, (
            "expected 2 scraper calls: initial SessionExpiredError + retry after reauth"
        )
        # reauth_callback called exactly once
        reauth_callback.assert_awaited_once()
        # No unhandled exception; no error entries
        assert errors == [], f"unexpected errors: {errors}"
        # Retry succeeded — timestamp advanced
        db.refresh(case)
        assert case.last_detail_checked_at is not None, "retry success must advance timestamp"
        assert before <= case.last_detail_checked_at <= after

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_reauth_fails_batch_stops_gracefully(self, db):
        """reauth_callback raises → batch stops; no unhandled exception; case_b untouched."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000002-2", name="Lawyer Reauth2", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="REAUTH-COURT2", name="Reauth Court 2", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_a = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-REAUTH-A", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_b = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-REAUTH-B", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_a, case_b])
        db.commit()

        api_a = _make_api_case("C-REAUTH-A", "token-a")
        api_b = _make_api_case("C-REAUTH-B", "token-b")

        mock_scraper = MagicMock()
        # Always raises SessionExpiredError
        mock_scraper.get_case_detail = AsyncMock(side_effect=SessionExpiredError("expired"))

        # reauth_callback raises — re-auth failure
        failing_reauth = AsyncMock(side_effect=RuntimeError("reauth_failed"))

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_a, api_b],
            selected_cases=[api_a, api_b],
            reauth_callback=failing_reauth,
        )

        # No unhandled exception (function returns normally)
        # Only case_a was attempted (scraper called once, then batch stopped)
        assert mock_scraper.get_case_detail.await_count == 1, (
            "batch must stop after first session error + failed reauth"
        )
        # case_a timestamp NOT advanced (session failure is not the case's fault)
        db.refresh(case_a)
        assert case_a.last_detail_checked_at is None, (
            "case_a timestamp must NOT be advanced when session error + reauth failure"
        )
        # case_b was not processed (batch stopped)
        db.refresh(case_b)
        assert case_b.last_detail_checked_at is None, "case_b must be untouched (batch stopped)"
        # Error captured
        assert len(errors) == 1, "one error entry for the stopped batch"

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_reauth_returns_none_batch_stops_gracefully(self, db):
        """reauth_callback returns None → batch stops; no unhandled exception."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000003-3", name="Lawyer Reauth3", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="REAUTH-COURT3", name="Reauth Court 3", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_a = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-RTNONE-A", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_b = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-RTNONE-B", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_a, case_b])
        db.commit()

        api_a = _make_api_case("C-RTNONE-A", "token-rtnone-a")
        api_b = _make_api_case("C-RTNONE-B", "token-rtnone-b")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=SessionExpiredError("expired"))

        # reauth_callback returns None — no new session available
        none_reauth = AsyncMock(return_value=None)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_a, api_b],
            selected_cases=[api_a, api_b],
            reauth_callback=none_reauth,
        )

        # Batch stops after case_a — scraper called once
        assert mock_scraper.get_case_detail.await_count == 1, "batch must stop; only case_a attempted"
        # No unhandled exception; error captured
        assert len(errors) == 1, "one error entry for the stopped batch"
        # case_b not processed
        db.refresh(case_b)
        assert case_b.last_detail_checked_at is None, "case_b must not be touched (batch stopped)"

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_session_error_does_not_advance_timestamp_with_reauth_failure(self, db):
        """SessionExpiredError + failing reauth → case timestamp is NOT advanced.

        Session failure is not the case's fault.  Even after a reauth attempt
        that fails, the rotation position (last_detail_checked_at) must be
        preserved so the case is retried on the next batch with a fresh session.
        """
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000004-4", name="Lawyer Reauth4", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="REAUTH-COURT4", name="Reauth Court 4", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-SESSEXP-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-SESSEXP-1", "token-sessexp")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=SessionExpiredError("expired"))

        failing_reauth = AsyncMock(side_effect=RuntimeError("credentials unavailable"))

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
            reauth_callback=failing_reauth,
        )

        db.refresh(case)
        # INVARIANT: session error must NOT advance last_detail_checked_at
        assert case.last_detail_checked_at is None, (
            "SessionExpiredError with failed reauth must NOT advance last_detail_checked_at; "
            "session failure is not the case's fault"
        )

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_reauth_succeeds_retry_generic_error_advances_timestamp_continues(self, db):
        """Path E: reauth OK, retry raises a generic (non-session) error.

        Per the case-specific-error rule: a generic exception on the retry advance
        last_detail_checked_at AND the loop continues to the next case (no break).
        """
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000010-0", name="Lawyer PathE", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="PATHE-COURT", name="Path E Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_a = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-PATHE-A", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_b = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-PATHE-B", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_a, case_b])
        db.commit()

        api_a = _make_api_case("C-PATHE-A", "token-pathe-a")
        api_b = _make_api_case("C-PATHE-B", "token-pathe-b")

        # call 1: SessionExpiredError (case_a initial)
        # call 2: RuntimeError (case_a retry after reauth)
        # call 3: success (case_b)
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=[
                SessionExpiredError("expired"),
                RuntimeError("parse failed"),
                _make_detail_empty(),
            ]
        )

        fresh_session = MagicMock()
        reauth_callback = AsyncMock(return_value=fresh_session)

        before = datetime.utcnow()
        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_a, api_b],
            selected_cases=[api_a, api_b],
            reauth_callback=reauth_callback,
        )
        after = datetime.utcnow()

        # 3 calls: session-error + retry for case_a, then case_b
        assert mock_scraper.get_case_detail.await_count == 3, (
            "expected 3 scraper calls: initial session error + retry (case_a) + case_b"
        )
        # Generic retry error → timestamp IS advanced (case-specific-error rule)
        db.refresh(case_a)
        assert case_a.last_detail_checked_at is not None, (
            "generic retry error must advance last_detail_checked_at"
        )
        assert before <= case_a.last_detail_checked_at <= after
        # Exactly 1 error recorded (case_a retry failure)
        assert len(errors) == 1, f"expected 1 error, got {len(errors)}: {errors}"
        # Loop continues — case_b processed
        db.refresh(case_b)
        assert case_b.last_detail_checked_at is not None, (
            "case_b must be processed — generic retry error must not stop the loop"
        )

    @pytest.mark.slice2
    @pytest.mark.asyncio
    async def test_reauth_succeeds_retry_session_errors_batch_stops(self, db):
        """Path D: reauth OK, retry raises SessionExpiredError.

        Double session error: timestamp is NOT advanced; batch stops gracefully
        (a subsequent case in the batch is NOT processed).
        """
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = Lawyer(rut="20000011-1", name="Lawyer PathD", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="PATHD-COURT", name="Path D Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_a = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-PATHD-A", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_b = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-PATHD-B", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_a, case_b])
        db.commit()

        api_a = _make_api_case("C-PATHD-A", "token-pathd-a")
        api_b = _make_api_case("C-PATHD-B", "token-pathd-b")

        # Both calls for case_a raise SessionExpiredError: initial + retry after reauth
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=[
                SessionExpiredError("expired"),
                SessionExpiredError("expired again"),
            ]
        )

        fresh_session = MagicMock()
        reauth_callback = AsyncMock(return_value=fresh_session)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_a, api_b],
            selected_cases=[api_a, api_b],
            reauth_callback=reauth_callback,
        )

        # Exactly 2 scraper calls: initial + retry for case_a; case_b never reached
        assert mock_scraper.get_case_detail.await_count == 2, (
            "expected 2 scraper calls: initial + retry after reauth; case_b must not be attempted"
        )
        # Timestamp NOT advanced — double session error is not the case's fault
        db.refresh(case_a)
        assert case_a.last_detail_checked_at is None, (
            "double session error must NOT advance last_detail_checked_at"
        )
        # Exactly 1 error recorded
        assert len(errors) == 1, f"expected 1 error, got {len(errors)}: {errors}"
        # Batch stopped — case_b untouched
        db.refresh(case_b)
        assert case_b.last_detail_checked_at is None, (
            "case_b must not be processed — batch stops after double session error"
        )
