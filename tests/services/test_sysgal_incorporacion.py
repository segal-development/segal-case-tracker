"""encolar_incorporaciones: which Sysgal causas enter the incorporation queue, and how."""

from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.services.sysgal_incorporacion import encolar_incorporaciones


@pytest.fixture
def court(db):
    c = Court(code="C4", name="4º Juzgado Civil de Santiago", region="RM", type="civil",
              pjud_corte=90, pjud_tribunal=4)
    db.add(c)
    db.flush()
    return c


def _causa(db, sid, rol="C-100-2026", tribunal="4° Juzgado Civil de Santiago", estado="activa",
           updated=datetime(2026, 11, 1), **kw):
    row = SysgalCausa(sysgal_id=sid, rol=rol, rol_norm=rol, tribunal=tribunal, estado=estado,
                      sysgal_updated_at=updated, synced_at=datetime.utcnow(), **kw)
    db.add(row)
    db.flush()
    return row


def _queue(db):
    return {q.sysgal_causa_id: q for q in db.query(SysgalIncorporacion).all()}


def test_enqueues_active_unmatched_causa_with_court_and_corte(db, court):
    c = _causa(db, 1)

    stats = encolar_incorporaciones(db)

    q = _queue(db)[c.id]
    assert (q.estado, q.intentos, q.court_id, q.corte, q.case_id) == ("pendiente", 0, court.id, 90, None)
    assert stats["encoladas"] == 1


def test_skips_ineligible_causas(db, court):
    lw = Lawyer(rut="16021492-9", name="Firm")
    db.add(lw)
    db.flush()
    case = Case(lawyer_id=lw.id, court_id=court.id, rol="C-5-2026", competencia="civil")
    db.add(case)
    db.flush()
    _causa(db, 1, estado="terminada")
    _causa(db, 2, eliminada=True)
    _causa(db, 3, rol="DEMANDA CUIDADO PERSONAL")
    _causa(db, 4, rol="C-5-2026", case_id=case.id, match_method="rol+tribunal")
    _causa(db, 5, match_method="ambigua")

    encolar_incorporaciones(db)

    assert _queue(db) == {}


def test_unresolvable_tribunal_is_sin_corte(db, court):
    c = _causa(db, 1, tribunal="Juzgado Inexistente")

    stats = encolar_incorporaciones(db)

    q = _queue(db)[c.id]
    assert (q.estado, q.court_id, q.corte) == ("sin_corte", None, None)
    assert stats["sin_corte"] == 1


def test_court_without_pjud_corte_is_sin_corte(db):
    db.add(Court(code="X", name="Juzgado Sin Corte", region="RM", type="civil"))
    c = _causa(db, 1, tribunal="Juzgado Sin Corte")

    encolar_incorporaciones(db)

    assert _queue(db)[c.id].estado == "sin_corte"


def test_tribunal_codigo_resolves_court(db, court):
    c = _causa(db, 1, tribunal="Nombre raro", tribunal_codigo="C4")

    encolar_incorporaciones(db)

    assert _queue(db)[c.id].court_id == court.id


def test_priority_year_desc_then_most_recently_updated(db, court):
    old = _causa(db, 1, rol="C-1-2024", updated=datetime(2026, 11, 9))
    a = _causa(db, 2, rol="C-2-2026", updated=datetime(2026, 1, 1))
    b = _causa(db, 3, rol="C-3-2026", updated=datetime(2026, 5, 1))
    mid = _causa(db, 4, rol="C-4-2025", updated=datetime(2026, 11, 9))

    encolar_incorporaciones(db)

    q = _queue(db)
    order = sorted(q, key=lambda k: q[k].prioridad)
    assert order == [b.id, a.id, mid.id, old.id]


def test_is_idempotent_and_keeps_existing_rows(db, court):
    c = _causa(db, 1)
    encolar_incorporaciones(db)
    row = _queue(db)[c.id]
    row.intentos, row.estado = 2, "error"
    db.flush()

    stats = encolar_incorporaciones(db)

    assert db.query(SysgalIncorporacion).count() == 1
    assert (_queue(db)[c.id].estado, _queue(db)[c.id].intentos) == ("error", 2)
    assert stats["encoladas"] == 0


def test_new_causas_rerank_pending_rows(db, court):
    first = _causa(db, 1, rol="C-1-2025")
    encolar_incorporaciones(db)
    newer = _causa(db, 2, rol="C-2-2026")

    encolar_incorporaciones(db)

    q = _queue(db)
    assert q[newer.id].prioridad < q[first.id].prioridad


def test_later_match_marks_incorporada_with_case_id(db, court):
    lw = Lawyer(rut="16021492-9", name="Firm")
    db.add(lw)
    db.flush()
    case = Case(lawyer_id=lw.id, court_id=court.id, rol="C-100-2026", competencia="civil")
    db.add(case)
    db.flush()
    c = _causa(db, 1)
    encolar_incorporaciones(db)
    c.case_id, c.match_method = case.id, "rol+tribunal"
    db.flush()

    stats = encolar_incorporaciones(db)

    q = _queue(db)[c.id]
    assert (q.estado, q.case_id) == ("incorporada", case.id)
    assert stats["incorporadas"] == 1


def test_sin_corte_row_becomes_pendiente_once_court_resolves(db):
    c = _causa(db, 1)
    encolar_incorporaciones(db)
    assert _queue(db)[c.id].estado == "sin_corte"
    db.add(Court(code="C4", name="4º Juzgado Civil de Santiago", region="RM", type="civil", pjud_corte=90))
    db.flush()

    encolar_incorporaciones(db)

    assert _queue(db)[c.id].estado == "pendiente"
    assert _queue(db)[c.id].corte == 90
