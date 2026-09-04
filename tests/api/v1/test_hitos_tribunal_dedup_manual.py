"""Tolerant tribunal dedup for MANUAL create/edit (not just import).

Hito identity is (abogado, RUT cliente, ROL, tribunal). A hito stored WITHOUT a
tribunal (created before migration 048) must still block a re-entry that now
carries one, and vice versa — otherwise the same causa is paid twice. This is how
Silvia's August 2026 duplicates were created: old hitos had tribunal=NULL, so
re-adding them WITH a tribunal did not collide. The bulk importer already handled
this; these tests pin the same rule on the create and edit endpoints.
"""

import pytest

from app.api.v1.hitos import _tribunal_collides
from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
CLIENT_RUT = "20.636.016-K"
ROL = "C-9826-2026"
TRIB = "3º Juzgado Civil de Santiago"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestTribunalCollides:
    def test_equal_tribunales_collide(self):
        assert _tribunal_collides("3º JCS", "3º JCS") is True

    def test_different_tribunales_do_not_collide(self):
        assert _tribunal_collides("3º JCS", "8º JCS") is False

    def test_either_side_blank_collides(self):
        assert _tribunal_collides(None, "3º JCS") is True
        assert _tribunal_collides("3º JCS", None) is True
        assert _tribunal_collides(None, None) is True


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="19813311-6", name="Silvia Barros Manríquez", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def tipo(db):
    t = HitoTipo(code="junior_h1_conversion", label="Conversión preventiva → M1 Alta",
                 nivel="junior", valor_bruto=2423, orden=1)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _seed(db, lawyer, tipo, tribunal):
    h = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=2423,
             fecha_hito=__import__("datetime").date(2026, 8, 6),
             rol_causa=CLIENT_RUT, descripcion=ROL, tribunal=tribunal, estado="aprobado")
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _create(client, tipo_id, lawyer_id, *, tribunal, rol=ROL, rut=CLIENT_RUT):
    return client.post(
        "/api/v1/hitos", headers=_h(ADMIN_RUT),
        data={"hito_tipo_id": tipo_id, "fecha_hito": "2026-08-31", "lawyer_id": lawyer_id,
              "rol_causa": rut, "descripcion": rol, "tribunal": tribunal or ""},
    )


class TestCreateTolerantDedup:
    def test_the_silvia_bug_stored_without_tribunal_blocks_new_with_tribunal(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=None)
        r = _create(client, tipo.id, lawyer.id, tribunal=TRIB)
        assert r.status_code == 409, r.text
        assert db.query(Hito).count() == 1

    def test_stored_with_tribunal_blocks_new_without(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=TRIB)
        r = _create(client, tipo.id, lawyer.id, tribunal=None)
        assert r.status_code == 409, r.text

    def test_same_tribunal_blocks(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=TRIB)
        assert _create(client, tipo.id, lawyer.id, tribunal=TRIB).status_code == 409

    def test_different_tribunal_is_allowed(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=TRIB)
        r = _create(client, tipo.id, lawyer.id, tribunal="8º Juzgado Civil de Santiago")
        assert r.status_code == 201, r.text
        assert db.query(Hito).count() == 2

    def test_different_causa_is_allowed(self, client, db, admin, lawyer, tipo):
        _seed(db, lawyer, tipo, tribunal=None)
        r = _create(client, tipo.id, lawyer.id, tribunal=TRIB, rol="C-9844-2026")
        assert r.status_code == 201, r.text


class TestEditTolerantDedup:
    def test_edit_into_a_no_tribunal_twin_is_blocked(self, client, db, admin, lawyer, tipo):
        # An existing hito without tribunal, plus another causa we will edit to collide.
        _seed(db, lawyer, tipo, tribunal=None)
        other = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=2423,
                     fecha_hito=__import__("datetime").date(2026, 8, 10),
                     rol_causa=CLIENT_RUT, descripcion="C-1111-2026", tribunal=TRIB, estado="aprobado")
        db.add(other)
        db.commit()
        db.refresh(other)
        r = client.put(f"/api/v1/hitos/{other.id}", headers=_h(ADMIN_RUT),
                       json={"hito_tipo_id": tipo.id, "fecha_hito": "2026-08-10",
                             "rol_causa": CLIENT_RUT, "descripcion": ROL, "tribunal": TRIB})
        assert r.status_code == 409, r.text
