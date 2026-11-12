"""S17: a Mis-causas ingest of an incorporated causa updates the SAME case and clears the flag."""

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
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


def test_mis_causas_ingest_updates_same_row_and_clears_flag(db, world):
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
    assert case.patrocinio_pendiente is False
    assert case.lawyer_id == eduardo.id
    assert db.query(Movement).filter_by(case_id=case.id).count() == 1


def test_mis_causas_ingest_matches_empty_cuaderno_variant_without_duplicate(db, world):
    _, eduardo, court = world
    case = _incorporada(db, eduardo, court, procedure="Principal")

    result = SyncService(db).sync_cases(eduardo.id, [_scraped(cuaderno="")], competencia="civil")

    assert [r.id for r in db.query(Case).all()] == [case.id]
    assert (result.cases_new, result.cases_updated) == (0, 1)
    db.refresh(case)
    assert case.patrocinio_pendiente is False
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
