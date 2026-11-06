"""Activity audit: views endpoint, admin read API (list + summary), labels."""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token, hash_password
from app.models.activity_event import ActivityEvent
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services import activity_audit
from app.services.activity_labels import label_for
from tests.conftest import TestingSessionLocal


@pytest.fixture(autouse=True)
def audit_on(monkeypatch):
    monkeypatch.setattr(activity_audit, "ENABLED", True)
    activity_audit.set_session_factory(TestingSessionLocal)
    yield
    activity_audit.set_session_factory(None)


def _lawyer(db, rut, email, role="lawyer", name="Ana Abogada"):
    obj = Lawyer(rut=rut, name=name, email=email, role=role, is_active=True,
                 password_hash=hash_password("x" * 10))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": f"Bearer {create_access_token({'sub': rut})}"}


def _case(db, lawyer, rol):
    court = db.query(Court).first()
    if court is None:
        court = Court(code="T1-ACT", name="Juzgado", region="RM", type="civil")
        db.add(court)
        db.commit()
    c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active",
             competencia="civil", plaintiff="P", defendant="D",
             created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _ev(db, lawyer=None, kind="action", created_at=None, **kw):
    e = ActivityEvent(
        lawyer_id=lawyer.id if lawyer else None,
        actor_name=lawyer.name if lawyer else None,
        actor_role=lawyer.role if lawyer else None,
        kind=kind,
        created_at=created_at or datetime.utcnow(),
        **kw,
    )
    db.add(e)
    db.commit()
    return e


# ---- views ----------------------------------------------------------------

def test_view_recorded_204(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    c = _case(db, lw, "C-1-2024")
    r = client.post("/api/v1/activity/views", json={"path": "/causas/1", "case_id": c.id},
                    headers=_h(lw.rut))
    assert r.status_code == 204
    db.expire_all()
    (ev,) = db.query(ActivityEvent).all()
    assert ev.kind == "view"
    assert ev.route == "/causas/1" and ev.path == "/causas/1"
    assert ev.case_id == c.id and ev.lawyer_id == lw.id
    assert ev.method is None


def test_view_requires_auth(client, db):
    r = client.post("/api/v1/activity/views", json={"path": "/causas"})
    assert r.status_code == 401
    assert db.query(ActivityEvent).count() == 0


def test_view_dedupe_30_seconds(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    for _ in range(2):
        assert client.post("/api/v1/activity/views", json={"path": "/plazos"},
                           headers=_h(lw.rut)).status_code == 204
    assert db.query(ActivityEvent).count() == 1
    # other path is not deduped
    client.post("/api/v1/activity/views", json={"path": "/hitos"}, headers=_h(lw.rut))
    assert db.query(ActivityEvent).count() == 2
    # old event no longer dedupes
    db.query(ActivityEvent).update({"created_at": datetime.utcnow() - timedelta(seconds=45)})
    db.commit()
    client.post("/api/v1/activity/views", json={"path": "/plazos"}, headers=_h(lw.rut))
    assert db.query(ActivityEvent).count() == 3


def test_view_dedupe_is_per_user(client, db):
    a = _lawyer(db, "11111111-1", "a@x.cl")
    b = _lawyer(db, "22222222-2", "b@x.cl", name="Beto")
    client.post("/api/v1/activity/views", json={"path": "/plazos"}, headers=_h(a.rut))
    client.post("/api/v1/activity/views", json={"path": "/plazos"}, headers=_h(b.rut))
    assert db.query(ActivityEvent).count() == 2


@pytest.mark.parametrize("body", [
    {"path": "causas"}, {"path": "/" + "a" * 200}, {}, {"path": "/x", "case_id": "abc"},
])
def test_view_validation(client, db, body):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    assert client.post("/api/v1/activity/views", json=body, headers=_h(lw.rut)).status_code == 422


# ---- read API --------------------------------------------------------------

def test_list_admin_only(client, db):
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    assert client.get("/api/v1/activity", headers=_h(lw.rut)).status_code == 403
    assert client.get("/api/v1/activity/summary", headers=_h(lw.rut)).status_code == 403
    assert client.get("/api/v1/activity").status_code == 401


def test_list_shape_newest_first_and_label(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    lw = _lawyer(db, "11111111-1", "a@x.cl")
    c = _case(db, lw, "C-77-2024")
    now = datetime.utcnow()
    _ev(db, lw, kind="login", route="/api/v1/auth/web-login", method="POST",
        path="/api/v1/auth/web-login", status_code=200, ip="1.2.3.4",
        created_at=now - timedelta(minutes=5))
    _ev(db, lw, kind="action", route="/api/v1/hitos", method="POST", path="/api/v1/hitos",
        status_code=201, case_id=c.id, target=None, created_at=now)
    body = client.get("/api/v1/activity", headers=_h(admin.rut)).json()
    assert body["total"] == 2 and body["page"] == 1 and body["pages"] == 1
    first, second = body["items"]
    assert first["kind"] == "action" and second["kind"] == "login"
    assert first["label"] == "Creó un hito"
    assert second["label"] == "Inició sesión"
    assert first["rol"] == "C-77-2024"
    assert second["rol"] is None
    assert first["created_at"].endswith("Z") or "+00:00" in first["created_at"]
    for k in ("id", "lawyer_id", "actor_name", "actor_role", "method", "route", "path",
              "status_code", "case_id", "target", "ip"):
        assert k in first
    assert second["ip"] == "1.2.3.4"


def test_list_filters_lawyer_kind_rol(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    a = _lawyer(db, "11111111-1", "a@x.cl")
    b = _lawyer(db, "22222222-2", "b@x.cl", name="Beto")
    c1 = _case(db, a, "C-100-2024")
    c2 = _case(db, a, "C-200-2024")
    _ev(db, a, kind="view", route="/causas/1", path="/causas/1", case_id=c1.id)
    _ev(db, b, kind="view", route="/causas/2", path="/causas/2", case_id=c2.id)
    _ev(db, b, kind="action", route="/api/v1/hitos", method="POST")
    h = _h(admin.rut)
    assert client.get(f"/api/v1/activity?lawyer_id={b.id}", headers=h).json()["total"] == 2
    assert client.get("/api/v1/activity?kind=view", headers=h).json()["total"] == 2
    r = client.get("/api/v1/activity?rol=c-200", headers=h).json()
    assert r["total"] == 1 and r["items"][0]["case_id"] == c2.id
    r = client.get(f"/api/v1/activity?lawyer_id={b.id}&kind=view", headers=h).json()
    assert r["total"] == 1


def test_list_days_are_chile_local_across_utc_midnight(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    # Santiago is UTC-3 in January (summer time).
    # 2025-01-15 02:30 UTC = 2025-01-14 23:30 Chile; 2025-01-15 03:30 UTC = 2025-01-15 00:30 Chile
    _ev(db, admin, kind="view", route="/a", path="/a", created_at=datetime(2025, 1, 15, 2, 30))
    _ev(db, admin, kind="view", route="/b", path="/b", created_at=datetime(2025, 1, 15, 3, 30))
    # 2025-01-16 02:59 UTC = 2025-01-15 23:59 Chile; 03:00 UTC = 2025-01-16 00:00 Chile
    _ev(db, admin, kind="view", route="/c", path="/c", created_at=datetime(2025, 1, 16, 2, 59))
    _ev(db, admin, kind="view", route="/d", path="/d", created_at=datetime(2025, 1, 16, 3, 0))
    h = _h(admin.rut)
    r = client.get("/api/v1/activity?desde=2025-01-15&hasta=2025-01-15", headers=h).json()
    assert sorted(i["route"] for i in r["items"]) == ["/b", "/c"]
    r = client.get("/api/v1/activity?desde=2025-01-15", headers=h).json()
    assert sorted(i["route"] for i in r["items"]) == ["/b", "/c", "/d"]
    r = client.get("/api/v1/activity?hasta=2025-01-14", headers=h).json()
    assert [i["route"] for i in r["items"]] == ["/a"]


def test_list_pagination(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    now = datetime.utcnow()
    for i in range(7):
        _ev(db, admin, kind="view", route=f"/p{i}", path=f"/p{i}",
            created_at=now - timedelta(seconds=i))
    h = _h(admin.rut)
    r = client.get("/api/v1/activity?per_page=3&page=2", headers=h).json()
    assert r["total"] == 7 and r["pages"] == 3 and r["page"] == 2
    assert [i["route"] for i in r["items"]] == ["/p3", "/p4", "/p5"]
    assert client.get("/api/v1/activity?per_page=101", headers=h).status_code == 422
    assert client.get("/api/v1/activity?page=0", headers=h).status_code == 422
    assert client.get("/api/v1/activity", headers=h).json()["items"].__len__() == 7


def test_summary_counts_per_person(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    a = _lawyer(db, "11111111-1", "a@x.cl")
    b = _lawyer(db, "22222222-2", "b@x.cl", name="Beto")
    # Chile day 2025-01-15 = 03:00Z on the 15th .. 02:59Z on the 16th
    d = datetime(2025, 1, 15, 12, 0)
    _ev(db, a, kind="view", route="/causas/1", path="/causas/1", case_id=1, created_at=d)
    _ev(db, a, kind="view", route="/causas/1", path="/causas/1", case_id=1,
        created_at=d + timedelta(minutes=1))
    _ev(db, a, kind="view", route="/causas/2", path="/causas/2", case_id=2,
        created_at=d + timedelta(minutes=2))
    _ev(db, a, kind="action", route="/api/v1/hitos", method="POST",
        created_at=d + timedelta(minutes=3))
    _ev(db, b, kind="view", route="/plazos", path="/plazos", created_at=d + timedelta(hours=1))
    # outside the Chile day (previous day 23:30 Chile) and anonymous failed login
    _ev(db, a, kind="view", route="/x", path="/x", created_at=datetime(2025, 1, 15, 2, 30))
    _ev(db, None, kind="login_failed", route="/api/v1/auth/web-login",
        created_at=d, identifier="zz@x.cl")
    rows = client.get("/api/v1/activity/summary?dia=2025-01-15", headers=_h(admin.rut)).json()
    assert [r["lawyer_id"] for r in rows] == [b.id, a.id]  # last_at desc
    ra = rows[1]
    assert ra["views"] == 3 and ra["actions"] == 1 and ra["cases_viewed"] == 2
    assert ra["actor_name"] == "Ana Abogada" and ra["actor_role"] == "lawyer"
    assert ra["first_at"] < ra["last_at"]
    rb = rows[0]
    assert rb["views"] == 1 and rb["actions"] == 0 and rb["cases_viewed"] == 0


def test_summary_defaults_to_today(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    _ev(db, admin, kind="view", route="/plazos", path="/plazos")
    rows = client.get("/api/v1/activity/summary", headers=_h(admin.rut)).json()
    assert len(rows) == 1 and rows[0]["views"] == 1


def test_activity_endpoints_do_not_audit_themselves(client, db):
    admin = _lawyer(db, "99999999-9", "adm@x.cl", role="admin", name="Admin")
    client.get("/api/v1/activity", headers=_h(admin.rut))
    assert db.query(ActivityEvent).count() == 0


# ---- labels ---------------------------------------------------------------

@pytest.mark.parametrize("kind,method,route,expected", [
    ("login", "POST", "/api/v1/auth/web-login", "Inició sesión"),
    ("login_failed", "POST", "/api/v1/auth/web-login", "Intento de inicio de sesión fallido"),
    ("view", None, "/causas/123", "Vio la causa"),
    ("view", None, "/causas", "Vio Causas"),
    ("view", None, "/plazos", "Vio Plazos"),
    ("view", None, "/hitos", "Vio Hitos"),
    ("view", None, "/agenda", "Vio Agenda"),
    ("view", None, "/productividad", "Vio Productividad"),
    ("action", "POST", "/api/v1/hitos", "Creó un hito"),
    ("action", "PUT", "/api/v1/hitos/{hito_id}", "Editó un hito"),
    ("action", "POST", "/api/v1/liberaciones", "Solicitó una liberación"),
    ("action", "POST", "/api/v1/liberaciones/{req_id}/aprobar", "Aprobó una liberación"),
    ("action", "POST", "/api/v1/liberaciones/{req_id}/rechazar", "Rechazó una liberación"),
    ("action", "PUT", "/api/v1/cases/{case_id}/deadlines/{deadline_id}/status",
     "Cambió el estado de un plazo"),
    ("action", "POST", "/api/v1/auth/change-password", "Cambió su contraseña"),
    ("action", "GET", "/api/v1/bono/liquidacion/export", "Exportó"),
    ("action", "POST", "/api/v1/unknown/thing", "POST /api/v1/unknown/thing"),
])
def test_labels(kind, method, route, expected):
    assert label_for(kind, method, route) == expected
