"""Blocking a person who no longer works at the firm.

A blocked person (is_active False) cannot sign in and an already issued session stops
working at once. A missing flag (None) still means active: some legacy accounts have it empty.
Blocking never deletes anything (cases, hitos, alerts and the bonus history stay).
"""
import pytest

from app.core.security import create_access_token, hash_password
from app.models.lawyer import Lawyer

PASSWORD = "una-clave-segura-1"
ADMIN_RUT = "16021492-9"


def _lawyer(db, rut, name, role="lawyer", email=None, **kw):
    obj = Lawyer(rut=rut, name=name, role=role, email=email, password_hash=hash_password(PASSWORD), **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


@pytest.fixture
def admin(db):
    return _lawyer(db, ADMIN_RUT, "Carla Admin", role="admin", email="carla@segal.cl")


@pytest.fixture
def ana(db):
    return _lawyer(db, "12345678-5", "Ana Perez", email="ana@segal.cl", is_active=True, hito_form_token="tok-ana-0123456789")


def _login(client, email, password=PASSWORD):
    return client.post("/api/v1/auth/web-login", json={"email": email, "password": password})


def _block(client, lawyer_id, value=False):
    return client.put(f"/api/v1/lawyers/{lawyer_id}/account", headers=_h(ADMIN_RUT), json={"is_active": value})


class TestSignIn:
    def test_active_person_signs_in(self, client, db, ana):
        assert _login(client, "ana@segal.cl").status_code == 200

    def test_a_missing_flag_still_means_active(self, client, db):
        _lawyer(db, "11111111-1", "Sin Marca", email="sm@segal.cl", is_active=None)

        assert _login(client, "sm@segal.cl").status_code == 200

    def test_blocked_person_is_refused_with_a_clear_message(self, client, db, ana):
        ana.is_active = False
        db.commit()

        r = _login(client, "ana@segal.cl")

        assert r.status_code == 403
        assert "bloqueada" in r.json()["detail"].lower()
        assert "access_token" not in r.text

    def test_a_wrong_password_never_reveals_the_block(self, client, db, ana):
        ana.is_active = False
        db.commit()

        r = _login(client, "ana@segal.cl", password="incorrecta")

        assert r.status_code == 401
        assert r.json()["detail"] == "Credenciales inválidas"


class TestIssuedSessions:
    def test_a_session_issued_before_the_block_stops_working(self, client, db, ana):
        assert client.get("/api/v1/auth/me", headers=_h(ana.rut)).status_code == 200
        ana.is_active = False
        db.commit()

        r = client.get("/api/v1/auth/me", headers=_h(ana.rut))

        assert r.status_code in (401, 403)
        assert "email" not in r.text

    def test_it_stops_on_other_routes_too(self, client, db, ana):
        ana.is_active = False
        db.commit()

        assert client.get("/api/v1/lawyers", headers=_h(ana.rut)).status_code in (401, 403)

    def test_a_token_whose_person_is_unknown_is_still_rejected_as_before(self, client, db):
        r = client.get("/api/v1/auth/me", headers=_h("99999999-9"))

        assert r.status_code in (401, 404)


class TestBlockEndpoint:
    def test_admin_blocks_and_the_response_says_so(self, client, db, admin, ana):
        r = _block(client, ana.id, False)

        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False
        db.refresh(ana)
        assert ana.is_active is False

    def test_blocking_revokes_the_public_hito_link(self, client, db, admin, ana):
        _block(client, ana.id, False)

        db.refresh(ana)
        assert ana.hito_form_token is None

    def test_blocking_keeps_everything_else(self, client, db, admin, ana):
        _block(client, ana.id, False)

        db.refresh(ana)
        assert ana.name == "Ana Perez" and ana.email == "ana@segal.cl" and ana.password_hash is not None
        assert db.query(Lawyer).filter(Lawyer.id == ana.id).count() == 1

    def test_reactivating_lets_her_sign_in_again(self, client, db, admin, ana):
        _block(client, ana.id, False)
        assert _login(client, "ana@segal.cl").status_code == 403

        r = _block(client, ana.id, True)

        assert r.status_code == 200 and r.json()["is_active"] is True
        assert _login(client, "ana@segal.cl").status_code == 200

    def test_a_plain_lawyer_cannot_block_anyone(self, client, db, admin, ana):
        other = _lawyer(db, "22222222-2", "Otro", email="otro@segal.cl")

        r = client.put(f"/api/v1/lawyers/{other.id}/account", headers=_h(ana.rut), json={"is_active": False})

        assert r.status_code == 403
        db.refresh(other)
        assert other.is_active is not False

    def test_an_admin_cannot_block_herself(self, client, db, admin):
        _lawyer(db, "33333333-3", "Otro Admin", role="admin", email="oa@segal.cl")

        r = _block(client, admin.id, False)

        assert r.status_code == 409
        db.refresh(admin)
        assert admin.is_active is not False

    def test_an_admin_can_block_another_admin(self, client, db, admin):
        other = _lawyer(db, "33333333-3", "Otro Admin", role="admin", email="oa@segal.cl")

        r = _block(client, other.id, False)

        assert r.status_code == 200 and r.json()["is_active"] is False

    def test_unknown_person_is_404(self, client, db, admin):
        assert _block(client, 999999, False).status_code == 404

    def test_other_fields_still_update_without_touching_the_flag(self, client, db, admin, ana):
        r = client.put(f"/api/v1/lawyers/{ana.id}/account", headers=_h(ADMIN_RUT), json={"email": "nueva@segal.cl"})

        assert r.status_code == 200
        db.refresh(ana)
        assert ana.email == "nueva@segal.cl" and ana.is_active is True


class TestList:
    def test_accounts_list_exposes_the_state_and_the_level(self, client, db, admin, ana):
        ana.nivel = "junior"
        ana.is_active = False
        db.commit()

        row = next(a for a in client.get("/api/v1/lawyers/accounts", headers=_h(ADMIN_RUT)).json() if a["id"] == ana.id)

        assert row["is_active"] is False and row["nivel"] == "junior"

    def test_a_missing_flag_is_listed_as_active(self, client, db, admin):
        old = _lawyer(db, "11111111-1", "Sin Marca", email="sm@segal.cl", is_active=None)

        row = next(a for a in client.get("/api/v1/lawyers/accounts", headers=_h(ADMIN_RUT)).json() if a["id"] == old.id)

        assert row["is_active"] is True
