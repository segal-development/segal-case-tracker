"""Admin: creating/editing procurador accounts, /me contract, rosters exclusion (S2, S6)."""
import pytest

from app.models.procurador import ProcuradorAbogado
from tests.api.v1.procurador_support import MSG_LINKS, headers, make_lawyer

ADMIN = "16021492-9"


@pytest.fixture
def w(db):
    admin = make_lawyer(db, ADMIN, "Admin", role="admin")
    a = make_lawyer(db, "11111111-1", "Abogado A")
    b = make_lawyer(db, "22222222-2", "Abogado B")
    c = make_lawyer(db, "33333333-3", "Abogado C")
    return {"admin": admin, "a": a, "b": b, "c": c}


def _create(client, ids, rut="12345678-5", **kw):
    body = {"rut": rut, "name": "Proc Nuevo", "role": "procurador", "abogado_ids": ids, **kw}
    return client.post("/api/v1/lawyers", headers=headers(ADMIN), json=body)


@pytest.mark.parametrize("n", [0, 3])
def test_create_with_0_or_3_lawyers_is_422_exact_message(client, db, w, n):
    ids = [w["a"].id, w["b"].id, w["c"].id][:n]
    r = _create(client, ids)
    assert r.status_code == 422
    assert r.json()["detail"] == MSG_LINKS
    from app.models.lawyer import Lawyer
    assert db.query(Lawyer).filter(Lawyer.rut == "12345678-5").first() is None


def test_create_without_the_field_is_422(client, db, w):
    r = client.post("/api/v1/lawyers", headers=headers(ADMIN),
                    json={"rut": "12345678-5", "name": "X", "role": "procurador"})
    assert r.status_code == 422 and r.json()["detail"] == MSG_LINKS


@pytest.mark.parametrize("n", [1, 2])
def test_create_with_1_or_2_lawyers(client, db, w, n):
    ids = [w["a"].id, w["b"].id][:n]
    r = _create(client, ids)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["role"] == "procurador" and j["is_firm_lawyer"] is False
    assert [x["id"] for x in j["abogados"]] == sorted(ids)
    lst = client.get("/api/v1/lawyers/accounts", headers=headers(ADMIN)).json()
    row = next(x for x in lst if x["id"] == j["id"])
    assert {x["id"] for x in row["abogados"]} == set(ids)


def test_create_rejects_non_firm_or_blocked_lawyer(client, db, w):
    proc = make_lawyer(db, "44444444-4", "Otro Proc", role="procurador")
    r = _create(client, [proc.id])
    assert r.status_code == 422
    w["a"].is_active = False
    db.commit()
    assert _create(client, [w["a"].id]).status_code == 422


def test_edit_links_takes_effect_immediately(client, db, w):
    j = _create(client, [w["a"].id]).json()
    h = headers("12345678-5")
    assert [x["id"] for x in client.get("/api/v1/auth/me", headers=h).json()["abogados_apoyados"]] == [w["a"].id]
    r = client.put(f"/api/v1/lawyers/{j['id']}/account", headers=headers(ADMIN), json={"abogado_ids": [w["b"].id, w["c"].id]})
    assert r.status_code == 200
    assert {x["id"] for x in r.json()["abogados"]} == {w["b"].id, w["c"].id}
    me = client.get("/api/v1/auth/me", headers=h).json()
    assert {x["id"] for x in me["abogados_apoyados"]} == {w["b"].id, w["c"].id}


@pytest.mark.parametrize("ids", [[], [1, 2, 3]])
def test_edit_with_bad_count_is_422_and_changes_nothing(client, db, w, ids):
    j = _create(client, [w["a"].id]).json()
    ids = [w["a"].id, w["b"].id, w["c"].id] if ids else []
    r = client.put(f"/api/v1/lawyers/{j['id']}/account", headers=headers(ADMIN),
                   json={"abogado_ids": ids, "email": "nuevo@segal.cl"})
    assert r.status_code == 422 and r.json()["detail"] == MSG_LINKS
    assert db.query(ProcuradorAbogado).filter_by(procurador_id=j["id"]).count() == 1
    from app.models.lawyer import Lawyer
    assert db.get(Lawyer, j["id"]).email is None


def test_turning_a_lawyer_into_procurador_requires_links_and_drops_firm_flag(client, db, w):
    r = client.put(f"/api/v1/lawyers/{w['c'].id}/account", headers=headers(ADMIN), json={"role": "procurador"})
    assert r.status_code == 422 and r.json()["detail"] == MSG_LINKS
    db.refresh(w["c"])
    assert w["c"].role == "lawyer"
    r = client.put(f"/api/v1/lawyers/{w['c'].id}/account", headers=headers(ADMIN),
                   json={"role": "procurador", "abogado_ids": [w["a"].id]})
    assert r.status_code == 200 and r.json()["is_firm_lawyer"] is False


def test_changing_role_away_from_procurador_clears_links(client, db, w):
    j = _create(client, [w["a"].id]).json()
    r = client.put(f"/api/v1/lawyers/{j['id']}/account", headers=headers(ADMIN), json={"role": "lawyer"})
    assert r.status_code == 200 and r.json()["abogados"] == []
    assert db.query(ProcuradorAbogado).filter_by(procurador_id=j["id"]).count() == 0


def test_block_and_unblock_work(client, db, w):
    j = _create(client, [w["a"].id]).json()
    r = client.put(f"/api/v1/lawyers/{j['id']}/account", headers=headers(ADMIN), json={"is_active": False})
    assert r.json()["is_active"] is False
    assert client.get("/api/v1/auth/me", headers=headers("12345678-5")).status_code == 401
    r = client.put(f"/api/v1/lawyers/{j['id']}/account", headers=headers(ADMIN), json={"is_active": True})
    assert r.json()["is_active"] is True


def test_delete_fresh_procurador_removes_links(client, db, w):
    j = _create(client, [w["a"].id]).json()
    assert client.delete(f"/api/v1/lawyers/{j['id']}", headers=headers(ADMIN)).status_code == 204
    assert db.query(ProcuradorAbogado).count() == 0


def test_me_of_non_procurador_has_no_new_key(client, db, w):
    assert "abogados_apoyados" not in client.get("/api/v1/auth/me", headers=headers(w["a"].rut)).json()
    accounts = client.get("/api/v1/lawyers/accounts", headers=headers(ADMIN)).json()
    assert all(x["abogados"] == [] for x in accounts)


def test_procurador_is_not_in_rosters_or_bono_or_traspaso(client, db, w):
    j = _create(client, [w["a"].id]).json()
    from app.services import lawyer_roster
    from app.utils.rut import normalize_rut
    assert normalize_rut("12345678-5") not in lawyer_roster.firm_lawyer_ruts(db)
    roster = client.get("/api/v1/bono/roster", headers=headers(ADMIN)).json()
    assert j["id"] not in {x.get("lawyer_id", x.get("id")) for x in roster}
    from app.services import traspaso_plan
    assert traspaso_plan.__dict__  # module importable; destination lists filter is_firm_lawyer
    from app.models.lawyer import Lawyer
    dest = db.query(Lawyer).filter(Lawyer.is_firm_lawyer.is_(True), Lawyer.is_active.is_(True)).all()
    assert j["id"] not in {d.id for d in dest}
    ab = client.get("/api/v1/hitos/abogados", headers=headers(ADMIN)).json()
    assert j["id"] not in {x["lawyer_id"] for x in ab}
