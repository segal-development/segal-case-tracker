# ruff: noqa: F811
"""S16: incorporated cases carry the full plaintiff/defendant (from litigantes, not the truncated caratulado)."""

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.scrapper.pjud.base import PJUDCase, PJUDCaseDetail, PJUDLitigante, PJUDMovement
from app.services.sysgal_incorporacion_pjud import reparar_caratulas, run_incorporacion
from tests.services.test_sysgal_incorporacion_pjud import (  # noqa: F401
    NOW,
    Fake,
    _item,
    _ritmo,
    court,
    lawyer,
)

BANCO = "BANCO DE CRÉDITO E INVERSIONES"
JUAN = "JUAN PÉREZ SOTO"


def _lit(role, name, rut="1-9"):
    return PJUDLitigante(participante=role, rut=rut, persona_type="JURIDICA", nombre=name)


def _detail(caratulado, litigantes):
    return PJUDCaseDetail(
        case=PJUDCase(rol="C-1-2026", tribunal="4º Juzgado Civil de Santiago", caratulado=caratulado,
                      fecha_ingreso="05/01/2026", estado_cuaderno="Tramitación", cuaderno="Principal"),
        movements=[PJUDMovement(folio="1", fecha="06/01/2026", tipo_tramite="Escrito", descripcion="mov",
                                etapa="Inicio", foja="1", tiene_documento=False)],
        litigantes=litigantes,
    )


async def _incorporate(db, detail):
    _ritmo(db)
    _item(db, db.query(Court).one(), 1)
    db.commit()
    await run_incorporacion(db, Fake(detail), notify=lambda *_: True, egress_ok=lambda: True, now=NOW)
    return db.query(Case).one()


@pytest.mark.asyncio
async def test_truncated_caratulado_is_replaced_by_litigantes(db, court, lawyer):
    detail = _detail("BANCO DE CRÉDITO E INVERSI",
                     [_lit("AB.DTE", "ABOGADO UNO"), _lit("DTE.", BANCO), _lit("DDO.", JUAN)])

    case = await _incorporate(db, detail)

    assert (case.plaintiff, case.defendant) == (BANCO, JUAN)


@pytest.mark.asyncio
async def test_multiple_parties_use_the_first_of_each_role(db, court, lawyer):
    detail = _detail("X", [_lit("DTE.", BANCO), _lit("DTE.", "OTRO"), _lit("DDO.", JUAN), _lit("DDO.", "OTRO2")])

    case = await _incorporate(db, detail)

    assert (case.plaintiff, case.defendant) == (BANCO, JUAN)


@pytest.mark.asyncio
async def test_without_litigantes_caratulado_split_is_kept(db, court, lawyer):
    case = await _incorporate(db, _detail("BANCO / PEREZ", []))

    assert (case.plaintiff, case.defendant) == ("BANCO", "PEREZ")


@pytest.mark.asyncio
async def test_missing_role_falls_back_per_side(db, court, lawyer):
    case = await _incorporate(db, _detail("BANCO / PEREZ", [_lit("DTE.", BANCO)]))

    assert (case.plaintiff, case.defendant) == (BANCO, "PEREZ")


def _case(db, court, lawyer, plaintiff, defendant, pendiente=True, rol="C-9-2026", lits=()):
    c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, plaintiff=plaintiff, defendant=defendant,
             status="active", procedure="Principal", competencia="civil", patrocinio_pendiente=pendiente)
    db.add(c)
    db.flush()
    for i, (role, name) in enumerate(lits):
        db.add(CaseLitigante(case_id=c.id, participante=role, rut="1-9", persona_type="JURIDICA",
                             nombre=name, natural_key=f"{c.id}-{i}"))
    db.flush()
    return c


def test_repair_fixes_truncated_plaintiff_and_empty_defendant(db, court, lawyer):
    c = _case(db, court, lawyer, "BANCO DE CRÉDITO E INVERSI", "", lits=[("DTE.", BANCO), ("DDO.", JUAN)])
    db.commit()

    stats = reparar_caratulas(db)

    db.refresh(c)
    assert (c.plaintiff, c.defendant) == (BANCO, JUAN)
    assert stats == {"revisadas": 1, "reparadas": 1, "sin_cambio": 0}


def test_repair_leaves_cases_when_db_has_nothing_better(db, court, lawyer):
    no_lits = _case(db, court, lawyer, "BANCO DE CRÉDITO E INVERSI", "", rol="C-1-2026")
    complete = _case(db, court, lawyer, BANCO, JUAN, rol="C-2-2026", lits=[("DTE.", BANCO), ("DDO.", JUAN)])
    not_pending = _case(db, court, lawyer, "BANCO", "", pendiente=False, rol="C-3-2026",
                        lits=[("DTE.", BANCO), ("DDO.", JUAN)])
    unrelated = _case(db, court, lawyer, "OTRO", "", rol="C-4-2026", lits=[("DTE.", BANCO)])
    db.commit()

    stats = reparar_caratulas(db)

    for c, expected in ((no_lits, ("BANCO DE CRÉDITO E INVERSI", "")), (complete, (BANCO, JUAN)),
                        (not_pending, ("BANCO", "")), (unrelated, ("OTRO", ""))):
        db.refresh(c)
        assert (c.plaintiff, c.defendant) == expected
    assert stats["reparadas"] == 0


def test_repair_is_idempotent(db, court, lawyer):
    _case(db, court, lawyer, "BANCO DE CRÉDITO E INVERSI", "", lits=[("DTE.", BANCO), ("DDO.", JUAN)])
    db.commit()

    first = reparar_caratulas(db)
    second = reparar_caratulas(db)

    assert first["reparadas"] == 1
    assert second["reparadas"] == 0
