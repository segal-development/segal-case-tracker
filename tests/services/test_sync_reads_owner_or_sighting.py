"""T3b: sync-path reads accept the case OWNER or a SIGHTING (case_lawyer_source).

Once new cases are owned by the firm lawyer, ``Case.lawyer_id == L`` matches
nothing for the individual lawyer L, so every read that picks "L's cases" must
also accept a ``case_lawyer_source`` row for L. Behavior is identical today
because every case has both.

For each read: (a) owner + sighting is selected, (b) firm-owned + sighting is
selected (new capability), (c) firm-owned without sighting is NOT selected,
(d) owner + sighting appears once (no duplicate rows).
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement

NOW = datetime(2026, 10, 14, 12, 0, 0)


@pytest.fixture
def world(db):
    """Individual lawyer A, firm lawyer F, one court."""
    a = Lawyer(rut="11111111-1", name="Lawyer A", is_active=True)
    f = Lawyer(rut="99999999-9", name="Firm F", is_active=True)
    court = Court(code="T3B", name="Tribunal T3b", region="RM", type="civil")
    db.add_all([a, f, court])
    db.commit()
    return a, f, court


def _case(db, court, owner, rol, *, sighted_by=None, checked=None):
    case = Case(
        lawyer_id=owner.id, court_id=court.id, rol=rol, competencia="civil",
        plaintiff="P", defendant="D", status="active",
        last_detail_checked_at=checked,
    )
    db.add(case)
    db.flush()
    if sighted_by is not None:
        db.add(CaseLawyerSource(case_id=case.id, lawyer_id=sighted_by.id))
    db.commit()
    return case


def _api(rol, token="tok"):
    m = MagicMock()
    m.rol = rol
    m.case_token = token
    return m


class TestHelper:
    def test_owner_sighting_firm_sighting_and_stranger(self, db, world):
        from app.services.sync_service import case_belongs_to_lawyer

        a, f, court = world
        own = _case(db, court, a, "C-1-2026", sighted_by=a)
        firm_seen = _case(db, court, f, "C-2-2026", sighted_by=a)
        _case(db, court, f, "C-3-2026")
        rows = (
            db.query(Case.id).filter(case_belongs_to_lawyer(a.id)).order_by(Case.id).all()
        )
        assert [r[0] for r in rows] == [own.id, firm_seen.id]


class TestRotationSelection:
    def _select(self, db, lawyer_id, rols):
        from app.services.sync_service import _select_cases_for_detail_rotation

        return [
            c.rol
            for c in _select_cases_for_detail_rotation(
                db, lawyer_id, "civil", [_api(r) for r in rols], batch_size=10
            )
        ]

    def test_owned_and_sighted_is_selected_once(self, db, world):
        a, _, court = world
        _case(db, court, a, "C-1-2026", sighted_by=a)
        assert self._select(db, a.id, ["C-1-2026"]) == ["C-1-2026"]

    def test_firm_owned_with_sighting_is_selected(self, db, world):
        a, f, court = world
        _case(db, court, f, "C-2-2026", sighted_by=a)
        assert self._select(db, a.id, ["C-2-2026"]) == ["C-2-2026"]

    def test_firm_owned_without_sighting_is_not_selected(self, db, world):
        a, f, court = world
        # A has another case so the empty-DB fallback to live cases is not hit.
        _case(db, court, a, "C-1-2026", sighted_by=a)
        _case(db, court, f, "C-3-2026")
        assert self._select(db, a.id, ["C-1-2026", "C-3-2026"]) == ["C-1-2026"]

    def test_owned_without_sighting_is_still_selected(self, db, world):
        a, _, court = world
        _case(db, court, a, "C-4-2026")
        assert self._select(db, a.id, ["C-4-2026"]) == ["C-4-2026"]


class TestOldestUncheckedLabel:
    def _label(self, db, lawyer_id):
        from app.services.sync_service import _oldest_unchecked_label

        return _oldest_unchecked_label(db, lawyer_id)

    def test_owned_and_sighted_counted_once(self, db, world):
        a, _, court = world
        _case(db, court, a, "C-1-2026", sighted_by=a)
        assert self._label(db, a.id).startswith("1 never-checked")

    def test_firm_owned_with_sighting_is_counted(self, db, world):
        a, f, court = world
        _case(db, court, f, "C-2-2026", sighted_by=a)
        _case(db, court, f, "C-5-2026", sighted_by=a, checked=NOW - timedelta(days=3))
        label = self._label(db, a.id)
        assert label.startswith("1 never-checked")
        assert "oldest-checked: none" not in label

    def test_firm_owned_without_sighting_is_not_counted(self, db, world):
        a, f, court = world
        _case(db, court, f, "C-3-2026")
        assert self._label(db, a.id) == "0 never-checked; oldest-checked: none"


class TestDetectAndSyncMovements:
    async def _run(self, db, lawyer_id, rol):
        from app.services.sync_service import detect_and_sync_movements

        mv = MagicMock()
        mv.folio, mv.fecha, mv.tipo_tramite = "001", "01/06/2024", "Resolución"
        mv.descripcion, mv.etapa, mv.foja = "Auto", "Cuaderno Principal", "1"
        mv.tiene_documento, mv.documento_token, mv.documentos = False, None, []
        detail = MagicMock()
        detail.case_documents = []
        detail.movements = [mv]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=detail)
        return await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(),
            lawyer_id=lawyer_id, api_cases=[_api(rol)],
        )

    @pytest.mark.asyncio
    async def test_firm_owned_with_sighting_stores_movements(self, db, world):
        a, f, court = world
        case = _case(db, court, f, "C-2-2026", sighted_by=a)
        new, _, errors = await self._run(db, a.id, "C-2-2026")
        assert new == 1 and not errors
        assert db.query(Movement).filter(Movement.case_id == case.id).count() == 1
        db.refresh(case)
        assert case.last_detail_checked_at is not None

    @pytest.mark.asyncio
    async def test_owned_and_sighted_stores_movements_once(self, db, world):
        a, _, court = world
        case = _case(db, court, a, "C-1-2026", sighted_by=a)
        new, _, errors = await self._run(db, a.id, "C-1-2026")
        assert new == 1 and not errors
        assert db.query(Movement).filter(Movement.case_id == case.id).count() == 1

    @pytest.mark.asyncio
    async def test_firm_owned_without_sighting_is_not_touched(self, db, world):
        a, f, court = world
        case = _case(db, court, f, "C-3-2026")
        new, _, _ = await self._run(db, a.id, "C-3-2026")
        assert new == 0
        assert db.query(Movement).filter(Movement.case_id == case.id).count() == 0


class TestClientSyncTagging:
    async def _sync(self, db, a, rol):
        from unittest.mock import patch

        from app.models.client import Client
        from app.services.client_sync import sync_one_cu_client
        from app.core.security import encrypt_pjud_password

        client = Client(
            rut="22222222-2", nombre="Cli",
            encrypted_clave_unica_password=encrypt_pjud_password("pw"),
            assigned_lawyer_id=a.id,
        )
        db.add(client)
        db.commit()
        api_case = MagicMock()
        api_case.rol = rol
        api_case.tribunal = "T"
        api_case.caratulado = "X con Y"
        api_case.fecha_ingreso = None
        api_case.estado_cuaderno = "Vigente"
        api_case.cuaderno = "Principal"
        api_case.institucion = None
        sc = MagicMock()
        sc._browser.new_context = AsyncMock(return_value=MagicMock(
            new_page=AsyncMock(return_value=MagicMock()), close=AsyncMock()))
        sc.start = AsyncMock()
        sc._page = None
        sc.get_my_cases = AsyncMock(return_value=[api_case])
        with (
            patch("app.scrapper.pjud.clave_unica.ClaveUnicaAuth.login",
                  new_callable=AsyncMock, return_value=MagicMock()),
            patch("app.services.sync_service.SyncService.sync_cases"),
            patch("app.services.sync_service._select_cases_for_detail_rotation",
                  return_value=[]),
            patch("app.services.sync_service.detect_and_sync_movements",
                  new_callable=AsyncMock, return_value=(0, 0, [])),
        ):
            await sync_one_cu_client(sc, db, client, batch=50)
        db.expire_all()
        return client

    @pytest.mark.asyncio
    async def test_firm_owned_with_sighting_is_tagged(self, db, world):
        a, f, court = world
        case = _case(db, court, f, "C-2-2026", sighted_by=a)
        client = await self._sync(db, a, "C-2-2026")
        assert db.get(Case, case.id).client_id == client.id

    @pytest.mark.asyncio
    async def test_owned_and_sighted_is_tagged(self, db, world):
        a, _, court = world
        case = _case(db, court, a, "C-1-2026", sighted_by=a)
        client = await self._sync(db, a, "C-1-2026")
        assert db.get(Case, case.id).client_id == client.id

    @pytest.mark.asyncio
    async def test_firm_owned_without_sighting_is_not_tagged(self, db, world):
        a, f, court = world
        case = _case(db, court, f, "C-3-2026")
        await self._sync(db, a, "C-3-2026")
        assert db.get(Case, case.id).client_id is None


class TestStalenessStats:
    def _stats(self, db, a, rols):
        from app.services import estado_diario_probe as probe

        return probe.staleness_stats(
            lambda: db, [probe.FeedRow(rol=r, tribunal=None) for r in rols], NOW, a.id
        )

    def test_owned_and_sighted_counted_once(self, db, world):
        a, _, court = world
        _case(db, court, a, "C-1-2026", sighted_by=a, checked=NOW - timedelta(days=2))
        s = self._stats(db, a, ["C-1-2026"])
        assert (s.in_db, s.not_in_db, s.other_lawyer) == (1, 0, 0)

    def test_firm_owned_with_sighting_is_in_db(self, db, world):
        a, f, court = world
        _case(db, court, f, "C-2-2026", sighted_by=a, checked=NOW - timedelta(days=2))
        s = self._stats(db, a, ["C-2-2026"])
        assert (s.in_db, s.not_in_db, s.other_lawyer) == (1, 0, 0)

    def test_firm_owned_without_sighting_is_other_lawyer(self, db, world):
        a, f, court = world
        _case(db, court, f, "C-3-2026")
        s = self._stats(db, a, ["C-3-2026"])
        assert (s.in_db, s.not_in_db, s.other_lawyer) == (0, 1, 1)


class TestStatusEndpoint:
    def _last_activity(self, client, a):
        r = client.get(
            "/api/v1/sync/status?competencia=civil",
            headers={"Authorization": "Bearer " + create_access_token({"sub": a.rut})},
        )
        assert r.status_code == 200
        return r.json()["last_activity"]

    def test_owned_and_sighted_is_reported(self, client, db, world):
        a, _, court = world
        _case(db, court, a, "C-1-2026", sighted_by=a, checked=NOW)
        assert self._last_activity(client, a) is not None

    def test_firm_owned_with_sighting_is_reported(self, client, db, world):
        a, f, court = world
        _case(db, court, f, "C-2-2026", sighted_by=a, checked=NOW)
        assert self._last_activity(client, a) is not None

    def test_firm_owned_without_sighting_is_not_reported(self, client, db, world):
        a, f, court = world
        _case(db, court, f, "C-3-2026", checked=NOW)
        assert self._last_activity(client, a) is None
