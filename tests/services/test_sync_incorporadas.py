"""S17: a Mis-causas ingest of an incorporated causa updates the SAME case and clears the flag."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.api.deps import resolve_case_scope
from app.services.lawyer_roster import case_ids_for_abogado
from app.services.sync_service import (
    ScrapedCase,
    ScrapedMovement,
    SyncService,
    clear_patrocinio_if_firm_litigante,
    contar_duplicados_incorporadas,
)

COURT = "4º Juzgado Civil de Santiago"
FIRM_RUT = "16021492-9"
ABOGADO_RUT = "12345678-5"


@pytest.fixture
def world(db):
    firm = Lawyer(rut=FIRM_RUT, name="Firm", is_active=True)
    eduardo = Lawyer(rut=ABOGADO_RUT, name="Eduardo", is_active=True)
    court = Court(code="C4", name=COURT, region="RM", type="civil")
    db.add_all([firm, eduardo, court])
    db.commit()
    return firm, eduardo, court


def _incorporada(db, eduardo, court, rol="C-6628-2026", procedure="Principal", pendiente=True):
    case = Case(lawyer_id=eduardo.id, court_id=court.id, rol=rol, competencia="civil", status="active",
                procedure=procedure, plaintiff="BANCO DE CRÉDITO E INVERSI", defendant="",
                patrocinio_pendiente=pendiente)
    db.add(case)
    db.commit()
    return case


def _scraped(rol="C-6628-2026", cuaderno="Principal"):
    return ScrapedCase(rol=rol, tribunal=COURT, caratulado="BANCO DE CREDITO E INVERSIONES/PEREZ",
                       fecha_ingreso="01/01/2026", estado_cuaderno="Tramitación", cuaderno=cuaderno)


def _mov(folio="1"):
    return ScrapedMovement(folio=folio, fecha="02/01/2026", tipo_tramite="Resolución",
                           descripcion="Se provee", etapa="Ingreso")


def test_mis_causas_ingest_updates_same_row_and_keeps_the_flag(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)
    svc = SyncService(db)
    svc.sync_movements(case.id, [_mov("1")], silent=True)

    result = SyncService(db).sync_cases(eduardo.id, [_scraped()], competencia="civil")
    svc.sync_movements(case.id, [_mov("1")])

    rows = db.query(Case).all()
    assert [r.id for r in rows] == [case.id]
    assert (result.cases_new, result.cases_updated) == (0, 1)
    db.refresh(case)
    assert case.patrocinio_pendiente is True
    assert case.lawyer_id == eduardo.id
    assert db.query(Movement).filter_by(case_id=case.id).count() == 1


def test_mis_causas_ingest_matches_empty_cuaderno_variant_without_duplicate(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court, procedure="Principal")

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="")], competencia="civil")

    assert [r.id for r in db.query(Case).all()] == [case.id]
    assert (result.cases_new, result.cases_updated) == (0, 1)
    db.refresh(case)
    assert case.patrocinio_pendiente is True
    assert case.procedure == "Principal"


def test_other_cuaderno_of_an_incorporated_rol_is_a_different_case_and_keeps_the_flag(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="Medida prejudicial")], competencia="civil")

    assert result.cases_new == 1
    db.refresh(case)
    assert case.patrocinio_pendiente is True


def test_never_incorporated_cases_behave_as_before(db, world):
    firm, eduardo, court = world
    regular = _incorporada(db, eduardo, court, rol="C-1-2025", pendiente=False)

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(rol="C-1-2025"), _scraped(rol="C-2-2025")],
                                        competencia="civil")

    assert (result.cases_new, result.cases_updated) == (1, 1)
    db.refresh(regular)
    assert regular.patrocinio_pendiente is False
    assert regular.lawyer_id == eduardo.id
    new = db.query(Case).filter_by(rol="C-2-2025").one()
    assert new.patrocinio_pendiente is False
    assert new.lawyer_id == firm.id


def _litigante(db, case, rut, participante="AB.DTE"):
    db.add(CaseLitigante(case_id=case.id, participante=participante, rut=rut, nombre="X",
                         persona_type="NATURAL", natural_key=f"{case.id}-{rut}-{participante}"))
    db.flush()


def test_firm_abogado_litigante_clears_the_flag(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)
    _litigante(db, case, "12.345.678-5")

    assert clear_patrocinio_if_firm_litigante(db, case) is True

    assert case.patrocinio_pendiente is False


@pytest.mark.parametrize("rut,participante", [("99999999-9", "AB.DTE"), (ABOGADO_RUT, "DTE."), (None, "AB.DTE")])
def test_foreign_or_non_abogado_litigante_keeps_the_flag(db, world, rut, participante):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)
    _litigante(db, case, rut or "", participante)

    assert clear_patrocinio_if_firm_litigante(db, case) is False

    assert case.patrocinio_pendiente is True


def test_clear_is_a_noop_for_non_pending_cases(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court, pendiente=False)
    _litigante(db, case, ABOGADO_RUT)

    assert clear_patrocinio_if_firm_litigante(db, case) is False


def test_contar_duplicados_incorporadas(db, world):
    firm, eduardo, court = world
    ok = _incorporada(db, eduardo, court, rol="C-1-2026")
    pending = _incorporada(db, eduardo, court, rol="C-2-2026", procedure="Principal")
    twin = Case(lawyer_id=firm.id, court_id=court.id, rol="C-2-2026", competencia="civil", procedure="",
                status="active")
    db.add(twin)
    db.commit()
    before = db.query(Case).count()

    pairs = contar_duplicados_incorporadas(db)

    assert pairs == [(pending.id, twin.id)]
    assert ok.id not in {i for p in pairs for i in p}
    assert db.query(Case).count() == before


def _mark_incorporada(db, case, n=1):
    causa = SysgalCausa(sysgal_id=n, rol=case.rol, rol_norm=case.rol, tribunal=COURT, case_id=case.id,
                        synced_at=datetime.utcnow())
    db.add(causa)
    db.flush()
    db.add(SysgalIncorporacion(sysgal_causa_id=causa.id, estado="incorporada", case_id=case.id))
    db.commit()


def test_two_empty_cuaderno_syncs_after_incorporation_keep_one_case(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)
    _mark_incorporada(db, case)

    first = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="")], competencia="civil")
    second = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="")], competencia="civil")

    assert [r.id for r in db.query(Case).all()] == [case.id]
    assert (first.cases_new, first.cases_updated) == (0, 1)
    assert (second.cases_new, second.cases_updated) == (0, 1)
    db.refresh(case)
    assert (case.patrocinio_pendiente, case.procedure) == (True, "Principal")


def test_never_incorporated_principal_with_empty_listing_row_is_unchanged_from_main(db, world):
    firm, eduardo, court = world
    regular = _incorporada(db, eduardo, court, pendiente=False)

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="")], competencia="civil")

    # Main keeps '' and 'Principal' as two identities: a second row is created under the firm owner.
    assert (result.cases_new, result.cases_updated) == (1, 0)
    assert db.query(Case).count() == 2
    db.refresh(regular)
    assert (regular.procedure, regular.patrocinio_pendiente) == ("Principal", False)


def test_year_floor_check_sees_incorporated_case_through_the_alias(db, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2026)
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court, rol="C-1-2020")
    _mark_incorporada(db, case)

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(rol="C-1-2020", cuaderno="")], competencia="civil")

    assert result.cases_year_skipped == 0
    assert (result.cases_new, result.cases_updated) == (0, 1)
    assert db.query(Case).count() == 1


@pytest.mark.asyncio
async def test_detail_sync_with_firm_abogado_clears_flag_and_keeps_owner(db, world, monkeypatch):
    import app.services.sync_service as mod
    from app.scrapper.pjud.base import PJUDLitigante

    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)
    monkeypatch.setattr(mod, "egress_allowed_cached", lambda: (True, "1.2.3.4", "allowed"))
    detail = MagicMock()
    detail.case_documents, detail.movements = [], []
    detail.notificaciones, detail.escritos, detail.exhortos = [], [], []
    detail.litigantes = [PJUDLitigante(participante="AB.DTE", rut=ABOGADO_RUT, persona_type="NATURAL",
                                       nombre="EDUARDO")]
    detail.case = MagicMock()
    scraper = MagicMock()
    scraper.get_case_detail = AsyncMock(return_value=detail)
    api_case = MagicMock()
    api_case.rol, api_case.case_token = case.rol, "t"

    await mod.detect_and_sync_movements(
        db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=eduardo.id,
        api_cases=[api_case], selected_cases=[api_case], reauth_callback=AsyncMock(),
    )

    db.refresh(case)
    assert case.patrocinio_pendiente is False
    assert case.lawyer_id == eduardo.id
    assert case.id in case_ids_for_abogado(db, ABOGADO_RUT, ABOGADO_RUT)


def test_case_stays_in_owner_scope_after_mis_causas_match_with_no_litigantes_stored(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court)

    SyncService(db).sync_cases(eduardo.id, [_scraped()], competencia="civil")
    db.expire_all()

    assert case.patrocinio_pendiente is True
    assert case.id in case_ids_for_abogado(db, ABOGADO_RUT, ABOGADO_RUT)
    assert case.id in resolve_case_scope(db, {"sub": ABOGADO_RUT})
