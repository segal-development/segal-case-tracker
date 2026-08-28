"""Tests for the evaluaciones (staff evaluation) module.

Covers criterios CRUD + admin gate, evaluables add/reactivate/soft-delete, the
/form endpoint, evaluation submit (happy path + validation), and resultados
averages that ignore N/A.
"""
from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.models.evaluacion import (
    Evaluacion,
    EvaluacionCriterio,
    EvaluacionEvaluable,
    EvaluacionRespuesta,
)
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"
MANAGER_RUT = "17555444-3"


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def abogado(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Eduardo Venegas", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def manager(db):
    """A plain lawyer granted the granular Evaluaciones permission (not admin)."""
    obj = Lawyer(rut=MANAGER_RUT, name="Ana Gestora", role="lawyer",
                 is_firm_lawyer=True, can_manage_evaluaciones=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def procurador(db):
    obj = Lawyer(rut="20613995-1", name="Camila Canales", role="procurador",
                 is_firm_lawyer=False, is_active=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _mk_criterio(db, label="Iniciativa", grupo="Criterios", orden=0,
                 permite_na=False, activo=True):
    c = EvaluacionCriterio(label=label, grupo=grupo, orden=orden,
                           permite_na=permite_na, activo=activo)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _mk_evaluable(db, lawyer_id, activo=True):
    e = EvaluacionEvaluable(lawyer_id=lawyer_id, activo=activo)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# --------------------------------------------------------------------------- #
# Criterios CRUD + admin gate
# --------------------------------------------------------------------------- #
def test_criterios_crud(client, admin, db):
    # create
    r = client.post("/api/v1/evaluaciones/criterios", headers=_h(ADMIN_RUT), json={
        "label": "Iniciativa", "grupo": "Criterios", "orden": 1,
    })
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["activo"] is True
    assert r.json()["permite_na"] is False

    # list (includes the new one)
    r = client.get("/api/v1/evaluaciones/criterios", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert any(c["id"] == cid for c in r.json())

    # update
    r = client.put(f"/api/v1/evaluaciones/criterios/{cid}", headers=_h(ADMIN_RUT), json={
        "label": "Iniciativa (edit)", "permite_na": True,
    })
    assert r.status_code == 200
    assert r.json()["label"] == "Iniciativa (edit)"
    assert r.json()["permite_na"] is True

    # soft delete → still listed but inactive
    r = client.delete(f"/api/v1/evaluaciones/criterios/{cid}", headers=_h(ADMIN_RUT))
    assert r.status_code == 204
    listed = client.get("/api/v1/evaluaciones/criterios", headers=_h(ADMIN_RUT)).json()
    match = [c for c in listed if c["id"] == cid]
    assert match and match[0]["activo"] is False


def test_criterios_ordered_by_grupo_orden_id(client, admin, db):
    _mk_criterio(db, label="B", grupo="Criterios", orden=1)
    _mk_criterio(db, label="A", grupo="Adicionales", orden=0)
    _mk_criterio(db, label="C", grupo="Criterios", orden=0)
    rows = client.get("/api/v1/evaluaciones/criterios", headers=_h(ADMIN_RUT)).json()
    labels = [c["label"] for c in rows]
    # Adicionales < Criterios (alphabetical grupo), then orden
    assert labels == ["A", "C", "B"]


def test_criterios_admin_gate(client, admin, abogado, db):
    # a role='lawyer' is forbidden
    assert client.get("/api/v1/evaluaciones/criterios", headers=_h(LAWYER_RUT)).status_code == 403
    assert client.post("/api/v1/evaluaciones/criterios", headers=_h(LAWYER_RUT),
                       json={"label": "X"}).status_code == 403


def test_update_delete_missing_criterio_404(client, admin):
    assert client.put("/api/v1/evaluaciones/criterios/999", headers=_h(ADMIN_RUT),
                      json={"label": "x"}).status_code == 404
    assert client.delete("/api/v1/evaluaciones/criterios/999",
                         headers=_h(ADMIN_RUT)).status_code == 404


# --------------------------------------------------------------------------- #
# Evaluables add / reactivate / soft-delete
# --------------------------------------------------------------------------- #
def test_evaluables_add_and_list(client, admin, abogado, procurador):
    r = client.post("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT),
                    json={"lawyer_id": procurador.id})
    assert r.status_code == 201
    assert r.json()["nombre"] == "Camila Canales"
    assert r.json()["role"] == "procurador"

    client.post("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT),
                json={"lawyer_id": abogado.id})
    rows = client.get("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT)).json()
    ids = {e["lawyer_id"] for e in rows}
    assert ids == {procurador.id, abogado.id}


def test_evaluables_add_missing_lawyer_404(client, admin):
    r = client.post("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT),
                    json={"lawyer_id": 9999})
    assert r.status_code == 404


def test_evaluables_reactivate_instead_of_duplicate(client, admin, procurador, db):
    # add, soft-delete, then re-add → same row reactivated (no duplicate)
    first = client.post("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT),
                        json={"lawyer_id": procurador.id}).json()
    client.delete(f"/api/v1/evaluaciones/evaluables/{first['id']}", headers=_h(ADMIN_RUT))
    again = client.post("/api/v1/evaluaciones/evaluables", headers=_h(ADMIN_RUT),
                        json={"lawyer_id": procurador.id}).json()
    assert again["id"] == first["id"]
    assert again["activo"] is True
    # only one row exists for this lawyer
    count = db.query(EvaluacionEvaluable).filter(
        EvaluacionEvaluable.lawyer_id == procurador.id).count()
    assert count == 1


def test_evaluables_soft_delete(client, admin, procurador, db):
    e = _mk_evaluable(db, procurador.id)
    r = client.delete(f"/api/v1/evaluaciones/evaluables/{e.id}", headers=_h(ADMIN_RUT))
    assert r.status_code == 204
    db.refresh(e)
    assert e.activo is False


def test_evaluables_admin_gate(client, admin, abogado):
    assert client.get("/api/v1/evaluaciones/evaluables",
                      headers=_h(LAWYER_RUT)).status_code == 403


# --------------------------------------------------------------------------- #
# /form
# --------------------------------------------------------------------------- #
def test_form_returns_active_criterios_and_evaluables(client, admin, abogado, procurador, db):
    _mk_criterio(db, label="Iniciativa", grupo="Criterios", orden=0)
    _mk_criterio(db, label="Inactivo", grupo="Criterios", orden=1, activo=False)
    _mk_evaluable(db, procurador.id, activo=True)
    _mk_evaluable(db, abogado.id, activo=False)

    # PUBLIC: anyone (no auth) can fetch the form
    r = client.get("/api/v1/evaluaciones/form")
    assert r.status_code == 200
    body = r.json()
    crit_labels = [c["label"] for c in body["criterios"]]
    assert crit_labels == ["Iniciativa"]  # inactive excluded
    ev_ids = [e["lawyer_id"] for e in body["evaluables"]]
    assert ev_ids == [procurador.id]  # inactive excluded


# --------------------------------------------------------------------------- #
# Submit evaluación
# --------------------------------------------------------------------------- #
def test_submit_happy_path(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa", permite_na=False)
    c2 = _mk_criterio(db, label="Agenda", grupo="Adicionales", permite_na=True)
    _mk_evaluable(db, procurador.id)

    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "Muy buen desempeño",
        "respuestas": [
            {"criterio_id": c1.id, "puntaje": 4},
            {"criterio_id": c2.id, "puntaje": None},  # N/A allowed
        ],
    })
    assert r.status_code == 201
    body = r.json()
    assert body["evaluado_lawyer_id"] == procurador.id
    assert body["evaluador_email"] == "eval@segal.cl"  # público, por email

    ev = db.query(Evaluacion).filter(Evaluacion.id == body["id"]).first()
    assert ev is not None
    assert ev.comentarios == "Muy buen desempeño"
    resp = db.query(EvaluacionRespuesta).filter(
        EvaluacionRespuesta.evaluacion_id == ev.id).all()
    assert {r.criterio_id: r.puntaje for r in resp} == {c1.id: 4, c2.id: None}


def test_submit_rejects_non_evaluable(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    # procurador is NOT in the evaluables list
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    })
    assert r.status_code == 400


def test_submit_rejects_inactive_evaluable(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id, activo=False)
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    })
    assert r.status_code == 400


def test_submit_rejects_out_of_range_puntaje(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 6}],
    })
    assert r.status_code == 400


def test_submit_rejects_null_on_non_permite_na(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa", permite_na=False)
    _mk_evaluable(db, procurador.id)
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": None}],
    })
    assert r.status_code == 400


def test_submit_rejects_inactive_criterio(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Viejo", activo=False)
    _mk_evaluable(db, procurador.id)
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    })
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Resultados — averages ignore N/A
# --------------------------------------------------------------------------- #
def test_resultados_averages_ignore_na(client, admin, abogado, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa", permite_na=False)
    c2 = _mk_criterio(db, label="Agenda", grupo="Adicionales", permite_na=True)
    _mk_evaluable(db, procurador.id)

    # eval 1: c1=4, c2=N/A ; eval 2: c1=2, c2=5
    for puntajes, comment in ([(4, None), "ok"], [(2, 5), "meh"]):
        db.add(Evaluacion(
            evaluado_lawyer_id=procurador.id,
            evaluador_email="eval@segal.cl",
            periodo="2026-08",
            comentarios=comment,
            respuestas=[
                EvaluacionRespuesta(criterio_id=c1.id, puntaje=puntajes[0]),
                EvaluacionRespuesta(criterio_id=c2.id, puntaje=puntajes[1]),
            ],
        ))
    db.commit()

    r = client.get("/api/v1/evaluaciones/resultados", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    rows = r.json()
    row = next(x for x in rows if x["lawyer_id"] == procurador.id)
    assert row["total_evaluaciones"] == 2
    per = {c["criterio_id"]: c for c in row["por_criterio"]}
    # c1: (4+2)/2 = 3.0, n=2
    assert per[c1.id]["promedio"] == 3.0
    assert per[c1.id]["n"] == 2
    # c2: only one non-null (5) → avg 5.0, n=1  (N/A ignored)
    assert per[c2.id]["promedio"] == 5.0
    assert per[c2.id]["n"] == 1
    # promedio_general over all non-null scores: (4+2+5)/3 = 3.67
    assert row["promedio_general"] == round((4 + 2 + 5) / 3, 2)
    assert set(row["comentarios"]) == {"ok", "meh"}


def test_resultados_admin_gate(client, admin, abogado):
    assert client.get("/api/v1/evaluaciones/resultados",
                      headers=_h(LAWYER_RUT)).status_code == 403


# --------------------------------------------------------------------------- #
# Monthly limit (1 per evaluador+evaluado+mes) + required comment
# --------------------------------------------------------------------------- #
def _second_evaluable(db):
    other = Lawyer(rut="18111111-1", name="Otro Evaluable", role="procurador",
                   is_firm_lawyer=False, is_active=True)
    db.add(other)
    db.commit()
    db.refresh(other)
    _mk_evaluable(db, other.id)
    return other


def test_submit_sets_periodo_current_month(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    r = client.post("/api/v1/evaluaciones", json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    })
    assert r.status_code == 201
    ev = db.query(Evaluacion).filter(Evaluacion.id == r.json()["id"]).first()
    assert ev.periodo == datetime.utcnow().strftime("%Y-%m")


def test_submit_monthly_limit_same_evaluador_evaluado(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    other = _second_evaluable(db)
    payload = {
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "Eval@segal.cl",  # normalized/lowercased by validator
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    }
    # first submit → 201
    assert client.post("/api/v1/evaluaciones", json=payload).status_code == 201
    # second submit, same evaluador+evaluado, same month → 409
    r2 = client.post("/api/v1/evaluaciones", json=payload)
    assert r2.status_code == 409
    assert "este mes" in r2.json()["detail"]
    # same evaluador → DIFFERENT evaluado, same month → allowed (201)
    r3 = client.post("/api/v1/evaluaciones", json={**payload, "evaluado_lawyer_id": other.id})
    assert r3.status_code == 201


def test_submit_requires_comentarios(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    base = {"evaluado_lawyer_id": procurador.id,
            "evaluador_email": "eval@segal.cl",
            "respuestas": [{"criterio_id": c1.id, "puntaje": 3}]}
    # missing comentarios → 422
    assert client.post("/api/v1/evaluaciones", json=base).status_code == 422
    # blank/whitespace comentarios → 422
    assert client.post("/api/v1/evaluaciones",
                       json={**base, "comentarios": "   "}).status_code == 422


# --------------------------------------------------------------------------- #
# Admin reset (free the evaluador+evaluado+mes slot)
# --------------------------------------------------------------------------- #
def test_reset_deletes_and_allows_resubmit(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    payload = {
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "comentarios": "ok",
        "respuestas": [{"criterio_id": c1.id, "puntaje": 3}],
    }
    first = client.post("/api/v1/evaluaciones", json=payload)
    assert first.status_code == 201
    ev_id = first.json()["id"]
    # a second submit is blocked this month
    assert client.post("/api/v1/evaluaciones", json=payload).status_code == 409

    periodo = datetime.utcnow().strftime("%Y-%m")
    r = client.post("/api/v1/evaluaciones/reset", headers=_h(ADMIN_RUT), json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "periodo": periodo,
    })
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # evaluation + its respuestas are gone
    assert db.query(Evaluacion).filter(Evaluacion.id == ev_id).first() is None
    assert db.query(EvaluacionRespuesta).filter(
        EvaluacionRespuesta.evaluacion_id == ev_id).count() == 0
    # slot freed → same evaluador can submit again this month
    assert client.post("/api/v1/evaluaciones", json=payload).status_code == 201


def test_reset_missing_404(client, admin, procurador, db):
    r = client.post("/api/v1/evaluaciones/reset", headers=_h(ADMIN_RUT), json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "nadie@segal.cl",
        "periodo": "2026-01",
    })
    assert r.status_code == 404


def test_reset_requires_admin(client, admin, abogado, procurador, db):
    r = client.post("/api/v1/evaluaciones/reset", headers=_h(LAWYER_RUT), json={
        "evaluado_lawyer_id": procurador.id,
        "evaluador_email": "eval@segal.cl",
        "periodo": "2026-01",
    })
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Resultados period filter + periodos list
# --------------------------------------------------------------------------- #
def test_resultados_filtered_by_periodo(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    # two evaluations in different months
    db.add(Evaluacion(
        evaluado_lawyer_id=procurador.id, evaluador_email="a@segal.cl",
        periodo="2026-07", comentarios="jul",
        respuestas=[EvaluacionRespuesta(criterio_id=c1.id, puntaje=2)],
    ))
    db.add(Evaluacion(
        evaluado_lawyer_id=procurador.id, evaluador_email="b@segal.cl",
        periodo="2026-08", comentarios="ago",
        respuestas=[EvaluacionRespuesta(criterio_id=c1.id, puntaje=4)],
    ))
    db.commit()

    # filter to 2026-08 only
    rows = client.get("/api/v1/evaluaciones/resultados?periodo=2026-08",
                      headers=_h(ADMIN_RUT)).json()
    row = next(x for x in rows if x["lawyer_id"] == procurador.id)
    assert row["total_evaluaciones"] == 1
    assert row["comentarios"] == ["ago"]
    per = {c["criterio_id"]: c for c in row["por_criterio"]}
    assert per[c1.id]["promedio"] == 4.0

    # no filter → both months aggregated
    rows_all = client.get("/api/v1/evaluaciones/resultados", headers=_h(ADMIN_RUT)).json()
    row_all = next(x for x in rows_all if x["lawyer_id"] == procurador.id)
    assert row_all["total_evaluaciones"] == 2


def test_periodos_distinct_desc(client, admin, procurador, db):
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    for per in ("2026-07", "2026-08", "2026-08"):
        db.add(Evaluacion(
            evaluado_lawyer_id=procurador.id, evaluador_email=f"{per}@segal.cl",
            periodo=per, comentarios="c",
            respuestas=[EvaluacionRespuesta(criterio_id=c1.id, puntaje=3)],
        ))
    db.commit()
    r = client.get("/api/v1/evaluaciones/periodos", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json() == ["2026-08", "2026-07"]


def test_periodos_admin_gate(client, admin, abogado):
    assert client.get("/api/v1/evaluaciones/periodos",
                      headers=_h(LAWYER_RUT)).status_code == 403


def test_submit_requires_valid_email(client, admin, procurador, db):
    """Público: el email del evaluador es obligatorio y debe ser válido."""
    c1 = _mk_criterio(db, label="Iniciativa")
    _mk_evaluable(db, procurador.id)
    base = {"evaluado_lawyer_id": procurador.id,
            "comentarios": "ok",
            "respuestas": [{"criterio_id": c1.id, "puntaje": 3}]}
    # sin email → 422
    assert client.post("/api/v1/evaluaciones", json=base).status_code == 422
    # email inválido → 422
    assert client.post("/api/v1/evaluaciones", json={**base, "evaluador_email": "no-es-email"}).status_code == 422
    # email válido → 201 (sin ningún header de auth: confirma que es público)
    assert client.post("/api/v1/evaluaciones", json={**base, "evaluador_email": "ok@segal.cl"}).status_code == 201


# --------------------------------------------------------------------------- #
# Granular permission: can_manage_evaluaciones (manage the module w/o being admin)
# --------------------------------------------------------------------------- #
def test_manager_can_read_resultados(client, manager, procurador, db):
    """A role='lawyer' WITH can_manage_evaluaciones reaches the admin endpoints."""
    _mk_evaluable(db, procurador.id)
    r = client.get("/api/v1/evaluaciones/resultados", headers=_h(MANAGER_RUT))
    assert r.status_code == 200


def test_manager_can_create_criterio(client, manager, db):
    r = client.post("/api/v1/evaluaciones/criterios", headers=_h(MANAGER_RUT),
                    json={"label": "Iniciativa", "grupo": "Criterios", "orden": 1})
    assert r.status_code == 201


def test_manager_can_manage_evaluables_and_periodos(client, manager, procurador, db):
    add = client.post("/api/v1/evaluaciones/evaluables", headers=_h(MANAGER_RUT),
                      json={"lawyer_id": procurador.id})
    assert add.status_code == 201
    assert client.get("/api/v1/evaluaciones/evaluables",
                      headers=_h(MANAGER_RUT)).status_code == 200
    assert client.get("/api/v1/evaluaciones/periodos",
                      headers=_h(MANAGER_RUT)).status_code == 200


def test_plain_lawyer_without_flag_forbidden(client, abogado, db):
    """A plain lawyer WITHOUT the flag is still 403 on the admin endpoints."""
    assert client.get("/api/v1/evaluaciones/resultados",
                      headers=_h(LAWYER_RUT)).status_code == 403
    assert client.post("/api/v1/evaluaciones/criterios", headers=_h(LAWYER_RUT),
                       json={"label": "X"}).status_code == 403
    assert client.get("/api/v1/evaluaciones/evaluables",
                      headers=_h(LAWYER_RUT)).status_code == 403


def test_admin_still_manages(client, admin, db):
    assert client.get("/api/v1/evaluaciones/resultados",
                      headers=_h(ADMIN_RUT)).status_code == 200


def test_me_includes_can_manage_evaluaciones(client, admin, manager, abogado):
    # manager: flag True
    r = client.get("/api/v1/auth/me", headers=_h(MANAGER_RUT))
    assert r.status_code == 200
    assert r.json()["can_manage_evaluaciones"] is True
    # plain lawyer: flag False
    r2 = client.get("/api/v1/auth/me", headers=_h(LAWYER_RUT))
    assert r2.status_code == 200
    assert r2.json()["can_manage_evaluaciones"] is False
