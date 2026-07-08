"""Tests for the in-app alert feed endpoints.

GET /api/v1/alerts, POST /api/v1/alerts/{id}/read, POST /api/v1/alerts/read-all.

Alerts are per-recipient (Alert.lawyer_id): every endpoint here is scoped
strictly to the CALLING lawyer's own alerts — a lawyer must never see or
mutate another lawyer's alert.
"""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.alert import Alert
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer

SANDY_RUT = "17171717-1"
CARLA_RUT = "16021492-9"


@pytest.fixture
def sandy(db):
    obj = Lawyer(rut=SANDY_RUT, name="Sandy", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def carla(db):
    obj = Lawyer(rut=CARLA_RUT, name="Carla", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-ALERTS", name="Juzgado Alerts", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def case(db, court):
    obj = Case(
        lawyer_id=1,
        court_id=court.id,
        rol="C-9001-2026",
        status="active",
        competencia="civil",
        plaintiff="DEMANDANTE",
        defendant="DEMANDADO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def _make_alert(
    db, lawyer_id, case_id, *, title="Alert", read=False, created_at=None, type="new_movement"
):
    alert = Alert(
        lawyer_id=lawyer_id,
        case_id=case_id,
        type=type,
        title=title,
        message="msg",
        read=read,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


class TestListAlerts:
    def test_returns_only_callers_alerts(self, client, db, sandy, carla, case):
        _make_alert(db, sandy.id, case.id, title="Sandy's alert")
        _make_alert(db, carla.id, case.id, title="Carla's alert")

        resp = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        assert resp.status_code == 200
        body = resp.json()
        titles = [item["title"] for item in body["items"]]
        assert titles == ["Sandy's alert"]

    def test_newest_first(self, client, db, sandy, case):
        base = datetime(2026, 1, 1, 12, 0, 0)
        _make_alert(db, sandy.id, case.id, title="Oldest", created_at=base)
        _make_alert(db, sandy.id, case.id, title="Newest", created_at=base + timedelta(days=2))
        _make_alert(db, sandy.id, case.id, title="Middle", created_at=base + timedelta(days=1))

        resp = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        titles = [item["title"] for item in resp.json()["items"]]
        assert titles == ["Newest", "Middle", "Oldest"]

    def test_pagination(self, client, db, sandy, case):
        base = datetime(2026, 1, 1, 12, 0, 0)
        for i in range(25):
            _make_alert(db, sandy.id, case.id, title=f"Alert {i}", created_at=base + timedelta(minutes=i))

        resp = client.get("/api/v1/alerts?page=1&per_page=20&category=all", headers=_headers(SANDY_RUT))
        body = resp.json()
        assert body["total"] == 25
        assert body["page"] == 1
        assert body["per_page"] == 20
        assert body["pages"] == 2
        assert len(body["items"]) == 20

        resp2 = client.get("/api/v1/alerts?page=2&per_page=20&category=all", headers=_headers(SANDY_RUT))
        body2 = resp2.json()
        assert len(body2["items"]) == 5

    def test_unread_only_filters_but_unread_count_is_total(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Read one", read=True)
        _make_alert(db, sandy.id, case.id, title="Unread one", read=False)
        _make_alert(db, sandy.id, case.id, title="Unread two", read=False)

        resp = client.get("/api/v1/alerts?unread_only=true&category=all", headers=_headers(SANDY_RUT))
        body = resp.json()
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Unread one", "Unread two"}
        assert body["unread_count"] == 2
        assert body["total"] == 2

        # unread_count stays correct even without the filter.
        resp_all = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        assert resp_all.json()["unread_count"] == 2
        assert resp_all.json()["total"] == 3

    def test_item_includes_case_context(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="With case")

        resp = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        item = resp.json()["items"][0]
        assert item["case_id"] == case.id
        assert item["case_rol"] == "C-9001-2026"
        assert item["case_caratulado"] == "DEMANDANTE/DEMANDADO"
        assert item["case_court_name"] == "Juzgado Alerts"
        assert item["read"] is False
        assert item["read_at"] is None

    def test_missing_case_does_not_crash(self, client, db, sandy, case):
        from sqlalchemy import text as sa_text

        alert = _make_alert(db, sandy.id, case.id, title="Orphan alert")
        case_id = case.id
        # Delete the case row directly (bypasses the ORM relationship, which
        # would otherwise null out alerts.case_id — a NOT NULL column —
        # before the parent delete). Simulates a case removed independently
        # of its alerts (e.g. hard-deleted or merged away).
        db.execute(sa_text("DELETE FROM cases WHERE id = :cid"), {"cid": case_id})
        db.commit()

        resp = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["case_id"] == case_id
        assert item["case_rol"] is None
        assert item["case_caratulado"] is None
        assert item["case_court_name"] is None


class TestListAlertsCategory:
    def test_default_category_is_actionable(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Movement", type="new_movement")
        _make_alert(db, sandy.id, case.id, title="Semaforo rojo", type="semaforo_rojo")

        resp = client.get("/api/v1/alerts", headers=_headers(SANDY_RUT))
        body = resp.json()
        titles = [item["title"] for item in body["items"]]
        assert titles == ["Semaforo rojo"]
        assert body["total"] == 1
        assert body["unread_count"] == 1

    def test_category_actionable_returns_only_actionable_types(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Movement", type="new_movement")
        _make_alert(db, sandy.id, case.id, title="Notificacion", type="new_notificacion")
        _make_alert(db, sandy.id, case.id, title="Semaforo rojo", type="semaforo_rojo")
        _make_alert(db, sandy.id, case.id, title="Deadline fatal", type="deadline_fatal")
        _make_alert(db, sandy.id, case.id, title="Deadline added", type="deadline_added")
        _make_alert(db, sandy.id, case.id, title="Deadline audit", type="deadline_audit")
        _make_alert(db, sandy.id, case.id, title="Credential change", type="credential_change")
        _make_alert(db, sandy.id, case.id, title="Status change", type="status_change")

        resp = client.get("/api/v1/alerts?category=actionable", headers=_headers(SANDY_RUT))
        body = resp.json()
        titles = {item["title"] for item in body["items"]}
        assert titles == {
            "Semaforo rojo",
            "Deadline fatal",
            "Deadline added",
            "Deadline audit",
            "Credential change",
            "Status change",
        }
        assert body["total"] == 6
        assert body["unread_count"] == 6

    def test_category_activity_returns_only_non_actionable_types(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Movement", type="new_movement")
        _make_alert(db, sandy.id, case.id, title="Notificacion", type="new_notificacion")
        _make_alert(db, sandy.id, case.id, title="Exhorto", type="new_exhorto")
        _make_alert(db, sandy.id, case.id, title="Escrito", type="new_escrito")
        _make_alert(db, sandy.id, case.id, title="Semaforo rojo", type="semaforo_rojo")

        resp = client.get("/api/v1/alerts?category=activity", headers=_headers(SANDY_RUT))
        body = resp.json()
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Movement", "Notificacion", "Exhorto", "Escrito"}
        assert body["total"] == 4
        assert body["unread_count"] == 4

    def test_category_all_returns_current_behavior(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Movement", type="new_movement", read=True)
        _make_alert(db, sandy.id, case.id, title="Semaforo rojo", type="semaforo_rojo")

        resp = client.get("/api/v1/alerts?category=all", headers=_headers(SANDY_RUT))
        body = resp.json()
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Movement", "Semaforo rojo"}
        assert body["total"] == 2
        assert body["unread_count"] == 1

    def test_unread_only_composes_with_category(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, title="Read actionable", type="semaforo_rojo", read=True)
        _make_alert(db, sandy.id, case.id, title="Unread actionable", type="deadline_fatal")
        _make_alert(db, sandy.id, case.id, title="Unread activity", type="new_movement")

        resp = client.get(
            "/api/v1/alerts?category=actionable&unread_only=true", headers=_headers(SANDY_RUT)
        )
        body = resp.json()
        titles = [item["title"] for item in body["items"]]
        assert titles == ["Unread actionable"]
        assert body["unread_count"] == 1
        assert body["total"] == 1

    def test_invalid_category_returns_422(self, client, db, sandy, case):
        resp = client.get("/api/v1/alerts?category=bogus", headers=_headers(SANDY_RUT))
        assert resp.status_code == 422

    def test_category_scoped_to_caller(self, client, db, sandy, carla, case):
        _make_alert(db, sandy.id, case.id, title="Sandy actionable", type="semaforo_rojo")
        _make_alert(db, carla.id, case.id, title="Carla actionable", type="semaforo_rojo")

        resp = client.get("/api/v1/alerts?category=actionable", headers=_headers(SANDY_RUT))
        titles = [item["title"] for item in resp.json()["items"]]
        assert titles == ["Sandy actionable"]


class TestMarkAlertRead:
    def test_marks_read_and_sets_read_at(self, client, db, sandy, case):
        alert = _make_alert(db, sandy.id, case.id)

        resp = client.post(f"/api/v1/alerts/{alert.id}/read", headers=_headers(SANDY_RUT))
        assert resp.status_code == 200
        body = resp.json()
        assert body["read"] is True
        assert body["read_at"] is not None

        db.refresh(alert)
        assert alert.read is True
        assert alert.read_at is not None

    def test_idempotent_on_second_call(self, client, db, sandy, case):
        alert = _make_alert(db, sandy.id, case.id)

        first = client.post(f"/api/v1/alerts/{alert.id}/read", headers=_headers(SANDY_RUT))
        second = client.post(f"/api/v1/alerts/{alert.id}/read", headers=_headers(SANDY_RUT))

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["read"] is True

    def test_another_lawyers_alert_returns_404(self, client, db, sandy, carla, case):
        alert = _make_alert(db, carla.id, case.id)

        resp = client.post(f"/api/v1/alerts/{alert.id}/read", headers=_headers(SANDY_RUT))
        assert resp.status_code == 404

    def test_unknown_alert_id_returns_404(self, client, sandy):
        resp = client.post("/api/v1/alerts/999999/read", headers=_headers(SANDY_RUT))
        assert resp.status_code == 404


class TestMarkAllAlertsRead:
    def test_marks_only_callers_unread_alerts(self, client, db, sandy, carla, case):
        _make_alert(db, sandy.id, case.id, title="Sandy unread 1")
        _make_alert(db, sandy.id, case.id, title="Sandy unread 2")
        _make_alert(db, sandy.id, case.id, title="Sandy already read", read=True)
        carla_alert = _make_alert(db, carla.id, case.id, title="Carla unread")

        resp = client.post("/api/v1/alerts/read-all", headers=_headers(SANDY_RUT))
        assert resp.status_code == 200
        assert resp.json() == {"marked": 2}

        db.refresh(carla_alert)
        assert carla_alert.read is False

    def test_second_call_marks_zero(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id)

        client.post("/api/v1/alerts/read-all", headers=_headers(SANDY_RUT))
        resp = client.post("/api/v1/alerts/read-all", headers=_headers(SANDY_RUT))

        assert resp.json() == {"marked": 0}

    def test_default_category_marks_everything(self, client, db, sandy, case):
        actionable = _make_alert(db, sandy.id, case.id, title="Actionable", type="semaforo_rojo")
        activity = _make_alert(db, sandy.id, case.id, title="Activity", type="new_movement")

        resp = client.post("/api/v1/alerts/read-all", headers=_headers(SANDY_RUT))
        assert resp.json() == {"marked": 2}

        db.refresh(actionable)
        db.refresh(activity)
        assert actionable.read is True
        assert activity.read is True

    def test_category_activity_marks_only_activity_unread(self, client, db, sandy, case):
        actionable = _make_alert(db, sandy.id, case.id, title="Actionable", type="semaforo_rojo")
        activity = _make_alert(db, sandy.id, case.id, title="Activity", type="new_movement")

        resp = client.post(
            "/api/v1/alerts/read-all?category=activity", headers=_headers(SANDY_RUT)
        )
        assert resp.json() == {"marked": 1}

        db.refresh(actionable)
        db.refresh(activity)
        assert actionable.read is False
        assert activity.read is True

    def test_category_actionable_marks_only_actionable_unread(self, client, db, sandy, case):
        actionable = _make_alert(db, sandy.id, case.id, title="Actionable", type="deadline_fatal")
        activity = _make_alert(db, sandy.id, case.id, title="Activity", type="new_notificacion")

        resp = client.post(
            "/api/v1/alerts/read-all?category=actionable", headers=_headers(SANDY_RUT)
        )
        assert resp.json() == {"marked": 1}

        db.refresh(actionable)
        db.refresh(activity)
        assert actionable.read is True
        assert activity.read is False

    def test_invalid_category_returns_422(self, client, sandy):
        resp = client.post(
            "/api/v1/alerts/read-all?category=bogus", headers=_headers(SANDY_RUT)
        )
        assert resp.status_code == 422
