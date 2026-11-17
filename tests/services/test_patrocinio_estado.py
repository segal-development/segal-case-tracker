"""Derivation of the Sysgal patrocinio/poder state from case data."""

from datetime import date, datetime

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.patrocinio_estado import derive_patrocinio_estado

FIRM_RUT = "16021492-9"


def _case(db, *, pendiente=False):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm", is_active=True)
    court = Court(code="PE1", name="Juzgado PE", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1-2026",
        status="active",
        patrocinio_pendiente=pendiente,
    )
    db.add(case)
    db.commit()
    return case


def _mv(db, case, day, description, procedure="Resolución"):
    db.add(
        Movement(
            case_id=case.id,
            movement_date=datetime(day.year, day.month, day.day),
            procedure=procedure,
            description=description,
            stage="Discusión",
        )
    )
    db.commit()


def _firm_litigante(db, case, participante="AB.DDO"):
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante=participante,
            rut="16.021.492-9",
            persona_type="NATURAL",
            nombre="Firm",
            natural_key=f"firm-{participante}",
        )
    )
    db.commit()


def _poder_flow(db, case):
    _mv(db, case, date(2026, 10, 1), "Opone excepciones", procedure="Escrito")
    _mv(db, case, date(2026, 10, 5), "Escrito patrocinio", procedure="Escrito")
    _mv(db, case, date(2026, 10, 10), "[760]Previo a proveer ratifique firma")


def test_no_case_is_sin_causa_pjud(db):
    result = derive_patrocinio_estado(db, None)
    assert result.estado == "sin_causa_pjud"
    assert result.poder_acreditado_at is None
    assert result.escrito_presentado_at is None


def test_patrocinio_pendiente_without_obligation_is_escrito_presentado(db):
    case = _case(db, pendiente=True)
    assert derive_patrocinio_estado(db, case).estado == "escrito_presentado"


def test_case_without_firm_litigante_nor_obligation_is_escrito_presentado(db):
    case = _case(db)
    assert derive_patrocinio_estado(db, case).estado == "escrito_presentado"


def test_open_obligation_is_poder_pendiente(db):
    case = _case(db, pendiente=True)
    _poder_flow(db, case)
    result = derive_patrocinio_estado(db, case)
    assert result.estado == "poder_pendiente"
    assert result.poder_acreditado_at is None
    # Latest firm escrito on/before the resolution that the court answered.
    assert result.escrito_presentado_at == datetime(2026, 10, 5)


def test_fulfilled_obligation_is_poder_acreditado_with_date(db):
    case = _case(db, pendiente=True)
    _poder_flow(db, case)
    _mv(db, case, date(2026, 10, 13), "Acredita Poder", procedure="Escrito")
    result = derive_patrocinio_estado(db, case)
    assert result.estado == "poder_acreditado"
    assert result.poder_acreditado_at == datetime(2026, 10, 13)


def test_firm_litigante_without_obligation_is_poder_acreditado(db):
    case = _case(db)
    _firm_litigante(db, case)
    result = derive_patrocinio_estado(db, case)
    assert result.estado == "poder_acreditado"
    assert result.poder_acreditado_at is None


def test_firm_litigante_ignored_while_patrocinio_pendiente(db):
    case = _case(db, pendiente=True)
    _firm_litigante(db, case)
    assert derive_patrocinio_estado(db, case).estado == "escrito_presentado"


def test_obligation_of_the_other_side_is_ignored(db):
    case = _case(db)
    _firm_litigante(db, case, participante="AB.DTE")  # firm is demandante
    _poder_flow(db, case)
    assert derive_patrocinio_estado(db, case).estado == "poder_acreditado"
