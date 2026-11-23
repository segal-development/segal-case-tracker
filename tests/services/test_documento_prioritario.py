"""Priority document requests in the station's case rotation and doc cap."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.services.sync_service import (
    _cap_historical_documents,
    _select_cases_for_detail_rotation,
)
from tests.services.test_detail_rotation import _make_api_case


def _setup(db, rut="00000077-7"):
    lawyer = Lawyer(rut=rut, name="Prio", is_active=True)
    db.add(lawyer)
    db.flush()
    court = db.query(Court).first()
    if not court:
        court = Court(code="PR-COURT", name="Prio Court", region="RM", type="civil")
        db.add(court)
        db.flush()
    return lawyer, court


def _case(db, lawyer, court, rol, filed_at=None):
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", last_detail_checked_at=None, filed_at=filed_at,
    )
    db.add(c)
    db.flush()
    return c


class TestRotationPriority:
    def test_case_with_priority_doc_goes_first_oldest_request_first(self, db):
        lawyer, court = _setup(db)
        now = datetime.utcnow()
        # filed_at DESC would normally order: C-A, C-B, C-C, C-D
        a = _case(db, lawyer, court, "C-1-2026", now)
        _case(db, lawyer, court, "C-2-2026", now - timedelta(days=1))
        c = _case(db, lawyer, court, "C-3-2026", now - timedelta(days=2))
        d = _case(db, lawyer, court, "C-4-2026", now - timedelta(days=3))
        db.add(Document(case_id=c.id, doc_type="resolution", status="pending",
                        prioridad_solicitada_at=now - timedelta(minutes=5)))
        db.add(Document(case_id=d.id, doc_type="resolution", status="failed",
                        prioridad_solicitada_at=now - timedelta(minutes=30)))
        # stored docs with a request must not count
        db.add(Document(case_id=a.id, doc_type="resolution", status="stored",
                        prioridad_solicitada_at=now - timedelta(hours=9)))
        db.commit()

        api = [_make_api_case(r) for r in
               ("C-1-2026", "C-2-2026", "C-3-2026", "C-4-2026")]
        result = _select_cases_for_detail_rotation(
            db, lawyer.id, "civil", api, batch_size=10
        )
        assert [ac.rol for ac in result] == [
            "C-4-2026", "C-3-2026", "C-1-2026", "C-2-2026"
        ]

    def test_no_priority_keeps_existing_order(self, db):
        lawyer, court = _setup(db, "00000078-8")
        now = datetime.utcnow()
        _case(db, lawyer, court, "C-1-2026", now)
        _case(db, lawyer, court, "C-2-2026", now - timedelta(days=1))
        db.commit()
        api = [_make_api_case("C-2-2026"), _make_api_case("C-1-2026")]
        result = _select_cases_for_detail_rotation(
            db, lawyer.id, "civil", api, batch_size=10
        )
        assert [ac.rol for ac in result] == ["C-1-2026", "C-2-2026"]


def _doc(i, *, requested=None, days_old=400, movement_id=None):
    return SimpleNamespace(
        id=i,
        document_date=datetime.utcnow() - timedelta(days=days_old),
        movement_id=movement_id,
        prioridad_solicitada_at=requested,
    )


class TestCapPriority:
    def test_priority_historical_kept_when_historical_disabled(self):
        prio = _doc(1, requested=datetime.utcnow())
        old = _doc(2)
        recent = _doc(3, days_old=1)
        kept = _cap_historical_documents(
            [old, prio, recent], recent_days=30, cap=0, rol="C-1-2026",
            historical_enabled=False,
        )
        assert {d.id for d in kept} == {1, 3}

    def test_priority_survives_cap_and_is_chosen_first(self):
        t = datetime.utcnow()
        prio = _doc(1, requested=t, days_old=900)
        newer = [_doc(i, days_old=100 + i) for i in range(2, 6)]
        kept = _cap_historical_documents(
            newer + [prio], recent_days=30, cap=2, rol="C-1-2026",
        )
        ids = [d.id for d in kept]
        assert 1 in ids
        assert len(ids) == 2  # counts against the cap
        assert ids[-1] == 1 or ids[0] == 1

    def test_priority_beyond_cap_all_kept_oldest_request_first(self):
        t = datetime.utcnow()
        p1 = _doc(1, requested=t - timedelta(minutes=1))
        p2 = _doc(2, requested=t - timedelta(minutes=9))
        kept = _cap_historical_documents(
            [p1, p2, _doc(3)], recent_days=30, cap=1, rol="C-1-2026",
        )
        assert [d.id for d in kept][:2] == [2, 1]
        assert 3 not in {d.id for d in kept}


# ---------------------------------------------------------------------------
# Year floor (DOC_DOWNLOAD_MIN_YEAR) must not block a requested document
# ---------------------------------------------------------------------------

_DL = "app.services.document_downloader.DocumentDownloader.download_and_store"


def _floor_setup(db, rol, requested_flags, rut="00000099-9"):
    """Case ``rol`` with one pending old doc per flag (True = user requested it)."""
    lawyer, court = _setup(db, rut)
    court.pjud_corte = 1  # sync_via_consulta skips courts without it
    case = _case(db, lawyer, court, rol)
    now = datetime.utcnow()
    docs = []
    for i, requested in enumerate(requested_flags):
        d = Document(
            case_id=case.id, doc_type="resolution", status="pending",
            pjud_token=f"t{i}", pjud_token_hash=f"floor-{rol}-{i}",
            document_date=now - timedelta(days=700 + i),
            prioridad_solicitada_at=(now - timedelta(days=4)) if requested else None,
        )
        db.add(d)
        docs.append(d)
    db.commit()
    return lawyer, case, docs


async def _run_detect(db, lawyer, rol, docs):
    from app.services import sync_service
    from app.services.sync_service import detect_and_sync_movements

    detail = MagicMock()
    detail.case_documents = []
    detail.movements = []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    detail.case = MagicMock()
    detail.case.rol = rol
    api_case = MagicMock()
    api_case.rol = rol
    api_case.case_token = "tok"
    scraper = MagicMock()
    scraper.get_case_detail = AsyncMock(return_value=detail)
    with patch.object(sync_service.settings, "DOC_DOWNLOAD_ENABLED", True), \
         patch.object(sync_service.settings, "DOC_DOWNLOAD_MIN_YEAR", 2024), \
         patch.object(sync_service.settings, "DOC_MAX_PER_CASE", 0), \
         patch.object(sync_service.settings, "DOC_HISTORICAL_ENABLED", True), \
         patch.object(sync_service.DocumentPersistenceService,
                      "persist_from_detail", return_value=docs), \
         patch(_DL, new_callable=AsyncMock) as dl:
        await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(),
            lawyer_id=lawyer.id, api_cases=[api_case], selected_cases=[api_case],
        )
    return dl.await_args.kwargs["pending_docs"] if dl.await_args_list else None


async def _run_consulta(db, lawyer, case, docs):
    from app.services import sync_service
    from app.services.sync_service import sync_via_consulta

    detail = MagicMock()
    detail.movements = []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(return_value=detail)
    with patch.object(sync_service.settings, "DOC_DOWNLOAD_ENABLED", True), \
         patch.object(sync_service.settings, "DOC_DOWNLOAD_MIN_YEAR", 2024), \
         patch.object(sync_service.DocumentPersistenceService,
                      "persist_from_detail", return_value=docs), \
         patch("app.services.sync_service._maybe_recompute_deadlines"), \
         patch(_DL, new_callable=AsyncMock) as dl:
        await sync_via_consulta(db, lawyer, scraper, MagicMock(), [case])
    return dl.await_args.kwargs["pending_docs"] if dl.await_args_list else None


@pytest.mark.asyncio
class TestYearFloorDetect:
    async def test_old_rol_requested_doc_is_downloaded(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2023", [True])
        sent = await _run_detect(db, lawyer, case.rol, docs)
        assert sent == [docs[0]]

    async def test_old_rol_without_request_is_not_downloaded(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2023", [False, False])
        assert await _run_detect(db, lawyer, case.rol, docs) is None

    async def test_old_rol_only_the_requested_doc_among_old_ones(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2023", [False, True, False])
        sent = await _run_detect(db, lawyer, case.rol, docs)
        assert sent == [docs[1]]

    async def test_recent_rol_downloads_everything_pending(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2025", [False, True, False])
        sent = await _run_detect(db, lawyer, case.rol, docs)
        assert sent is not None and {d.id for d in sent} == {d.id for d in docs}


@pytest.mark.asyncio
class TestYearFloorConsulta:
    async def test_old_rol_requested_doc_is_downloaded(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2023", [True], "00000098-8")
        assert await _run_consulta(db, lawyer, case, docs) == [docs[0]]

    async def test_old_rol_without_request_is_not_downloaded(self, db):
        lawyer, case, docs = _floor_setup(db, "C-10371-2023", [False], "00000097-7")
        assert await _run_consulta(db, lawyer, case, docs) is None

    async def test_old_rol_only_the_requested_doc_among_old_ones(self, db):
        lawyer, case, docs = _floor_setup(
            db, "C-10371-2023", [False, True, False], "00000096-6")
        assert await _run_consulta(db, lawyer, case, docs) == [docs[1]]

    async def test_recent_rol_downloads_everything_pending(self, db):
        lawyer, case, docs = _floor_setup(
            db, "C-10371-2025", [False, True, False], "00000095-5")
        sent = await _run_consulta(db, lawyer, case, docs)
        assert sent is not None and {d.id for d in sent} == {d.id for d in docs}
