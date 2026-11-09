"""GET /sysgal/cartera/reporte and PUT /sysgal/usuarios/{id} — admin only, read-only report."""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario

ADMIN_RUT, AUDITOR_RUT, LAWYER_RUT = "11111111-1", "33333333-3", "22222222-2"


def _headers(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))}


@pytest.fixture
def people(db):
    db.add_all([Lawyer(rut=ADMIN_RUT, name="Admin", role="admin"),
                Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor"),
                Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer")])
    db.commit()


@pytest.fixture
def admin(people):
    return _headers(ADMIN_RUT)


def _lawyer(db, rut, name, email=None):
    lw = Lawyer(rut=rut, name=name, email=email, role="lawyer")
    db.add(lw)
    db.flush()
    return lw


def _case(db, owner, rol, court=None, litigante_rut=None):
    court = court or Court(code=f"C{rol}", name="Juzgado", region="RM", type="civil")
    if court.id is None:
        db.add(court)
        db.flush()
    case = Case(lawyer_id=owner.id, court_id=court.id, rol=rol, competencia="civil")
    db.add(case)
    db.flush()
    if litigante_rut:
        db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=litigante_rut,
                             persona_type="NATURAL", nombre="X", natural_key=f"{rol}-{litigante_rut}"))
    return case


def _causa(db, sid, abogado, case=None, method=None, rol="C-1-2026", eliminada=False):
    db.add(SysgalCausa(sysgal_id=sid, rol=rol, rol_norm=rol, tribunal="T", abogado_sysgal_id=abogado,
                       case_id=case.id if case else None, match_method=method, eliminada=eliminada,
                       synced_at=datetime.utcnow()))


class TestAuth:
    @pytest.mark.parametrize("rut", [LAWYER_RUT, AUDITOR_RUT])
    def test_report_forbidden_for_non_admin(self, client, people, rut):
        assert client.get("/api/v1/sysgal/cartera/reporte", headers=_headers(rut)).status_code == 403

    @pytest.mark.parametrize("rut", [LAWYER_RUT, AUDITOR_RUT])
    def test_put_forbidden_for_non_admin(self, client, people, rut):
        r = client.put("/api/v1/sysgal/usuarios/1", json={"lawyer_id": None}, headers=_headers(rut))
        assert r.status_code == 403


class TestReporte:
    def test_empty(self, client, admin):
        body = client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()
        assert body["usuarios"] == [] and body["por_abogado"] == [] and body["ejemplos_sin_calce"] == []
        assert body["calce"] == {"total": 0, "calzadas": 0, "sin_calce": 0, "ambiguas": 0}

    def test_calce_counts_and_examples(self, client, admin, db):
        owner = _lawyer(db, "44444444-4", "Owner")
        case = _case(db, owner, "C-1-2026")
        _causa(db, 1, 9, case=case, method="rol+tribunal", rol="C-1-2026")
        _causa(db, 2, 9, rol="C-2-2026")
        _causa(db, 3, 9, method="ambigua", rol="C-3-2026")
        db.commit()

        body = client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()

        assert body["calce"] == {"total": 3, "calzadas": 1, "sin_calce": 1, "ambiguas": 1}
        assert [e["sysgal_id"] for e in body["ejemplos_sin_calce"]] == [2]
        assert body["ejemplos_sin_calce"][0]["rol"] == "C-2-2026"

    def test_examples_capped_at_20(self, client, admin, db):
        for i in range(25):
            _causa(db, 100 + i, 9, rol=f"C-{i}-2026")
        db.commit()
        body = client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()
        assert len(body["ejemplos_sin_calce"]) == 20 and body["calce"]["sin_calce"] == 25

    def test_usuarios_with_proposals(self, client, admin, db):
        by_mail = _lawyer(db, "55555555-5", "Otra Persona", email="Ana@Firm.cl")
        by_name = _lawyer(db, "66666666-6", "José Muñoz Pérez")
        db.add_all([
            SysgalUsuario(sysgal_id=1, nombre="Ana X", perfil="Abogado", estado="activo", correo="ana@firm.cl"),
            SysgalUsuario(sysgal_id=2, nombre="  jose  MUNOZ perez ", estado="activo"),
            SysgalUsuario(sysgal_id=3, nombre="Nadie", estado="activo"),
            SysgalUsuario(sysgal_id=4, nombre="Mapeado", lawyer_id=by_name.id),
        ])
        db.commit()

        us = {u["sysgal_id"]: u for u in client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()["usuarios"]}

        assert us[1]["propuesta_lawyer_id"] == by_mail.id and us[1]["lawyer_id"] is None
        assert us[2]["propuesta_lawyer_id"] == by_name.id
        assert us[3]["propuesta_lawyer_id"] is None
        assert (us[4]["lawyer_id"], us[4]["lawyer_nombre"]) == (by_name.id, "José Muñoz Pérez")

    def test_ambiguous_name_gives_no_proposal(self, client, admin, db):
        _lawyer(db, "55555555-5", "Juan Soto")
        _lawyer(db, "66666666-6", "juan  SOTO")
        db.add(SysgalUsuario(sysgal_id=1, nombre="Juan Soto"))
        db.commit()
        us = client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()["usuarios"]
        assert us[0]["propuesta_lawyer_id"] is None

    def test_por_abogado_hoy_vs_con_sysgal(self, client, admin, db):
        ana = _lawyer(db, "55555555-5", "Ana")
        beto = _lawyer(db, "66666666-6", "Beto")
        # Today: Ana is abogado-of-record on c1 and c2.
        c1 = _case(db, ana, "C-1-2026", litigante_rut=ana.rut)
        c2 = _case(db, ana, "C-2-2026", litigante_rut=ana.rut)
        # Sysgal says Beto is responsible for c1 and c2 matched; c3 eliminada is ignored.
        c3 = _case(db, ana, "C-3-2026")
        db.add(SysgalUsuario(sysgal_id=9, nombre="Beto S", lawyer_id=beto.id))
        _causa(db, 1, 9, case=c1, method="rol+tribunal")
        _causa(db, 2, 9, case=c2, method="rol+tribunal")
        _causa(db, 3, 9, case=c3, method="rol+tribunal", eliminada=True)
        db.commit()

        rows = {r["lawyer_id"]: r for r in client.get("/api/v1/sysgal/cartera/reporte", headers=admin).json()["por_abogado"]}

        assert (rows[ana.id]["hoy"], rows[ana.id]["con_sysgal"], rows[ana.id]["diferencia"]) == (2, 0, -2)
        assert (rows[beto.id]["hoy"], rows[beto.id]["con_sysgal"], rows[beto.id]["diferencia"]) == (0, 2, 2)
        assert rows[beto.id]["nombre"] == "Beto"

    def test_report_changes_nothing(self, client, admin, db):
        owner = _lawyer(db, "44444444-4", "Owner")
        case = _case(db, owner, "C-1-2026")
        db.add(SysgalUsuario(sysgal_id=9, nombre="Z"))
        _causa(db, 1, 9, case=case, method="rol+tribunal")
        db.commit()
        client.get("/api/v1/sysgal/cartera/reporte", headers=admin)
        db.expire_all()
        assert db.query(Case).get(case.id).assigned_lawyer_id is None
        assert db.query(SysgalUsuario).one().lawyer_id is None


class TestPutUsuario:
    def test_sets_and_clears_mapping(self, client, admin, db):
        lw = _lawyer(db, "55555555-5", "Ana")
        db.add(SysgalUsuario(sysgal_id=9, nombre="Ana S"))
        db.commit()

        r = client.put("/api/v1/sysgal/usuarios/9", json={"lawyer_id": lw.id}, headers=admin)
        assert r.status_code == 200 and r.json()["lawyer_id"] == lw.id
        db.expire_all()
        row = db.query(SysgalUsuario).one()
        assert (row.lawyer_id, row.mapped_by) == (lw.id, ADMIN_RUT)

        r = client.put("/api/v1/sysgal/usuarios/9", json={"lawyer_id": None}, headers=admin)
        assert r.status_code == 200 and r.json()["lawyer_id"] is None
        db.expire_all()
        assert db.query(SysgalUsuario).one().lawyer_id is None

    def test_unknown_usuario_404(self, client, admin):
        assert client.put("/api/v1/sysgal/usuarios/404", json={"lawyer_id": None}, headers=admin).status_code == 404

    def test_unknown_lawyer_404_and_mapping_unchanged(self, client, admin, db):
        db.add(SysgalUsuario(sysgal_id=9, nombre="Ana S"))
        db.commit()
        assert client.put("/api/v1/sysgal/usuarios/9", json={"lawyer_id": 99999}, headers=admin).status_code == 404
        db.expire_all()
        assert db.query(SysgalUsuario).one().lawyer_id is None
