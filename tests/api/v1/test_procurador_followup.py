"""Follow-up hardening for the procurador profile (S2, S3, S5, S6, S8)."""
import pytest

from app.api.deps import PROCURADOR_ALLOWED
from app.main import app
from app.models.document import Document
from app.models.hito import HITO_RECHAZADO, Hito, HitoTipo
from app.models.procurador import ProcuradorAbogado
from app.models.sysgal_usuario import SysgalUsuario
from app.services import storage_service
from tests.api.v1.procurador_support import headers, link, make_case, make_court, make_lawyer

ADMIN = "16021492-9"
MSG_EDIT = "Solo puedes editar o eliminar hitos pendientes que ingresaste tú."


class _Backend:
    def upload(self, data, key, content_type="application/pdf"):
        return key

    def retrieve(self, key):
        return b"x"


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: _Backend())


@pytest.fixture
def w(db):
    admin = make_lawyer(db, ADMIN, "Admin", role="admin")
    a = make_lawyer(db, "11111111-1", "Abogado A")
    b = make_lawyer(db, "22222222-2", "Abogado B")
    p = make_lawyer(db, "44444444-4", "Proc Uno", role="procurador")
    p2 = make_lawyer(db, "55555555-5", "Proc Dos", role="procurador")
    link(db, p, a)
    link(db, p2, a)
    t = HitoTipo(code="t1", label="Tipo 1", nivel="pleno", valor_bruto=1000, orden=1)
    db.add(t)
    db.commit()
    return {"admin": admin, "a": a, "b": b, "p": p, "p2": p2, "tipo": t}


def _mk_hito(client, rut, tipo, rol="C-1-2026", **extra):
    data = {"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15", "rol_causa": "12.345.678-5",
            "descripcion": rol, "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo", **extra}
    r = client.post("/api/v1/hitos", headers=headers(rut), data=data,
                    files={"evidencia": ("c.png", b"\x89PNG", "image/png")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put_ev(client, rut, hid):
    return client.put(f"/api/v1/hitos/{hid}/evidencia", headers=headers(rut),
                      files={"evidencia": ("n.png", b"\x89PNG2", "image/png")})


# 1. evidence replacement only while pending
def test_procurador_replaces_evidence_while_pending(client, db, w):
    hid = _mk_hito(client, w["p"].rut, w["tipo"])
    assert _put_ev(client, w["p"].rut, hid).status_code == 200


@pytest.mark.parametrize("estado", [HITO_RECHAZADO, "sugerido"])
def test_procurador_cannot_replace_evidence_when_not_pending(client, db, w, estado):
    hid = _mk_hito(client, w["p"].rut, w["tipo"])
    h = db.get(Hito, hid)
    h.estado = estado
    db.commit()
    before = h.evidencia_storage_key
    r = _put_ev(client, w["p"].rut, hid)
    assert r.status_code == 403 and r.json()["detail"] == MSG_EDIT
    db.refresh(h)
    assert h.evidencia_storage_key == before


def test_admin_can_still_replace_evidence_on_rejected_hito(client, db, w):
    hid = _mk_hito(client, w["p"].rut, w["tipo"])
    db.get(Hito, hid).estado = HITO_RECHAZADO
    db.commit()
    assert _put_ev(client, w["admin"].rut, hid).status_code == 200


# 2. account role transitions
def _upd(client, id_, **body):
    return client.put(f"/api/v1/lawyers/{id_}/account", headers=headers(ADMIN), json=body)


def test_demoting_procurador_to_lawyer_restores_firm_flag(client, db, w):
    r = _upd(client, w["p"].id, role="lawyer")
    assert r.status_code == 200 and r.json()["is_firm_lawyer"] is True
    db.refresh(w["p"])
    assert w["p"].is_firm_lawyer is True


def test_promoting_forces_firm_flag_false_and_nivel_none(client, db, w):
    w["b"].nivel = "pleno"
    db.commit()
    r = _upd(client, w["b"].id, role="procurador", abogado_ids=[w["a"].id])
    assert r.status_code == 200 and r.json()["is_firm_lawyer"] is False
    db.refresh(w["b"])
    assert w["b"].nivel is None and w["b"].is_firm_lawyer is False


def test_setting_nivel_on_procurador_is_422_and_changes_nothing(client, db, w):
    r = _upd(client, w["p"].id, nivel="pleno")
    assert r.status_code == 422 and r.json()["detail"] == "Un procurador no tiene nivel de bono."
    db.refresh(w["p"])
    assert w["p"].nivel is None


def test_nivel_alongside_promotion_is_422_and_changes_nothing(client, db, w):
    r = _upd(client, w["b"].id, role="procurador", abogado_ids=[w["a"].id], nivel="junior")
    assert r.status_code == 422 and r.json()["detail"] == "Un procurador no tiene nivel de bono."
    db.refresh(w["b"])
    assert w["b"].role == "lawyer"
    assert db.query(ProcuradorAbogado).filter_by(procurador_id=w["b"].id).count() == 0


def test_update_of_a_plain_lawyer_without_nivel_is_unchanged(client, db, w):
    r = _upd(client, w["a"].id, email="a@x.cl")
    assert r.status_code == 200 and r.json()["is_firm_lawyer"] is True


# 3. never a sync target / credential audit / sync-status
def test_procurador_not_a_sync_target(db, w):
    from app.workers.sync_scheduler import _active_lawyer_ids_by_staleness
    ids = _active_lawyer_ids_by_staleness(db)
    assert w["a"].id in ids and w["p"].id not in ids and w["p2"].id not in ids


def test_procurador_not_in_credential_audit(db, w):
    from app.services.credential_audit import credential_status, scan_credential_changes
    assert w["p"].id not in {x.get("lawyer_id", x.get("id")) for x in credential_status(db)}
    scan_credential_changes(db)  # smoke: still runs


def test_procurador_not_in_sync_status(client, db, w):
    from app.models.case import Case
    court = make_court(db)
    for owner in (w["a"], w["p"]):
        db.add(Case(lawyer_id=owner.id, court_id=court.id, rol=f"C-{owner.id}-2024", competencia="civil"))
    db.commit()
    r = client.get("/api/v1/lawyers/sync-status", headers=headers(ADMIN))
    assert r.status_code == 200
    assert {x["id"] for x in r.json()} == {w["a"].id}


# 4. regression tests
def test_every_api_route_is_gated_and_allowed_templates_exist():
    from fastapi.routing import APIRoute
    from app.api.deps import procurador_gate

    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1")]
    assert routes
    ungated = [
        (sorted(r.methods), r.path) for r in routes
        if procurador_gate not in {d.call for d in r.dependant.dependencies}
    ]
    assert ungated == []
    existing = {(m, r.path[len("/api/v1"):]) for r in routes for m in r.methods}
    assert PROCURADOR_ALLOWED - existing == set()


def test_deactivated_linked_lawyer_leaves_scope_immediately(client, db, w):
    court = make_court(db)
    ca = make_case(db, w["admin"], court, "C-1-2024", w["a"])
    cb = make_case(db, w["admin"], court, "C-2-2024", w["b"])
    link(db, w["p"], w["b"])
    h = headers(w["p"].rut)
    got = lambda: {i["id"] for i in client.get("/api/v1/cases", headers=h, params={"per_page": 100}).json()["items"]}  # noqa: E731
    assert got() == {ca.id, cb.id}
    w["b"].is_active = False
    db.commit()
    assert got() == {ca.id}


def test_document_and_evidence_outside_union(client, db, w):
    court = make_court(db)
    cb = make_case(db, w["admin"], court, "C-2-2024", w["b"])
    doc = Document(case_id=cb.id, filename="x.pdf", content_type="application/pdf", status="stored", gcs_path="k")
    db.add(doc)
    db.commit()
    h = headers(w["p"].rut)
    assert client.get(f"/api/v1/documents/{doc.id}", headers=h, follow_redirects=False).status_code == 404
    assert client.get(f"/api/v1/documents/{doc.id}/download", headers=h).status_code == 404
    other = _mk_hito(client, w["admin"].rut, w["tipo"], rol="C-9-2026", lawyer_id=w["b"].id)
    assert client.get(f"/api/v1/hitos/{other}/evidencia", headers=h).status_code == 403


def test_facets_abogados_empty_for_procurador(client, db, w):
    court = make_court(db)
    make_case(db, w["admin"], court, "C-1-2024", w["a"])
    r = client.get("/api/v1/cases/facets", headers=headers(w["p"].rut))
    assert r.status_code == 200 and r.json()["abogados"] == []


def test_other_procurador_or_admin_hito_is_403_on_put_and_delete(client, db, w):
    mine = _mk_hito(client, w["p"].rut, w["tipo"])
    admins = _mk_hito(client, w["admin"].rut, w["tipo"], rol="C-5-2026", lawyer_id=w["a"].id)
    body = {"hito_tipo_id": w["tipo"].id, "fecha_hito": "2026-07-16", "rol_causa": "12.345.678-5",
            "descripcion": "C-1-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo"}
    for hid in (mine, admins):
        h2 = headers(w["p2"].rut) if hid == mine else headers(w["p"].rut)
        assert client.put(f"/api/v1/hitos/{hid}", headers=h2, json=body).status_code == 403
        assert client.delete(f"/api/v1/hitos/{hid}", headers=h2).status_code == 403
        assert db.get(Hito, hid) is not None


# Sysgal mapping excludes procuradores
def test_sysgal_mapping_excludes_procuradores(client, db, w):
    w["p"].email = "proc@x.cl"
    db.add(SysgalUsuario(sysgal_id=7, nombre="Proc Uno", correo="proc@x.cl"))
    db.commit()
    r = client.get("/api/v1/sysgal/cartera/reporte", headers=headers(ADMIN))
    assert r.status_code == 200
    assert r.json()["usuarios"][0]["propuesta_lawyer_id"] is None
    r = client.put("/api/v1/sysgal/usuarios/7", headers=headers(ADMIN), json={"lawyer_id": w["p"].id})
    assert r.status_code == 404
    assert db.query(SysgalUsuario).one().lawyer_id is None
    r = client.put("/api/v1/sysgal/usuarios/7", headers=headers(ADMIN), json={"lawyer_id": w["a"].id})
    assert r.status_code == 200
