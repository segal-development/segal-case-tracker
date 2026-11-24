"""GET /api/v1/admin/pjud/actividad — admin only, per day/hour/IP/action/account."""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.pjud_request_count import PjudRequestCount

URL = "/api/v1/admin/pjud/actividad"


def _headers(db, rut, role):
    lw = Lawyer(rut=rut, name=f"N-{role}", email=f"{role}@segal.cl", role=role)
    db.add(lw)
    db.commit()
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}, lw


def _row(db, hour, lawyer_id, ip, action, kind, n):
    db.add(PjudRequestCount(hour_utc=hour, lawyer_id=lawyer_id, egress_ip=ip,
                            action=action, kind=kind, count=n))


def test_admin_gets_aggregates(client, db):
    h, lw = _headers(db, "11111111-1", "admin")
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    old = now - timedelta(days=30)
    _row(db, now, lw.id, "1.1.1.1", "detail", "request", 10)
    _row(db, now, lw.id, "1.1.1.1", "list", "request", 2)
    _row(db, now, None, "2.2.2.2", "login", "request", 1)
    _row(db, now, lw.id, "1.1.1.1", "detail", "shape_challenge", 3)
    _row(db, old, lw.id, "1.1.1.1", "detail", "request", 99)
    db.commit()

    r = client.get(URL + "?days=7", headers=h)
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"days", "total_requests", "total_rejections", "per_day", "per_hour",
                      "by_ip", "by_action", "by_account", "rejections"}
    assert b["days"] == 7
    assert b["total_requests"] == 13 and b["total_rejections"] == 3
    assert b["per_day"] == [{"day": now.date().isoformat(), "requests": 13, "rejections": 3}]
    assert b["per_hour"] == [{"hour_utc": now.isoformat(), "requests": 13, "rejections": 3}]
    assert {x["egress_ip"]: x["requests"] for x in b["by_ip"]} == {"1.1.1.1": 12, "2.2.2.2": 1}
    assert {x["action"]: x["requests"] for x in b["by_action"]} == {"detail": 10, "list": 2, "login": 1}
    acc = {x["lawyer_id"]: x for x in b["by_account"]}
    assert acc[lw.id]["requests"] == 12 and acc[lw.id]["name"] == "N-admin"
    assert acc[None]["requests"] == 1 and acc[None]["name"] is None
    assert b["rejections"] == [{"kind": "shape_challenge", "egress_ip": "1.1.1.1",
                                "action": "detail", "lawyer_id": lw.id, "count": 3}]


def test_days_validated(client, db):
    h, _ = _headers(db, "11111111-1", "admin")
    assert client.get(URL + "?days=0", headers=h).status_code == 422
    assert client.get(URL, headers=h).json()["days"] == 7


@pytest.mark.parametrize("role", ["lawyer", "auditor", "procurador"])
def test_other_roles_get_403(client, db, role):
    h, _ = _headers(db, "22222222-2", role)
    assert client.get(URL, headers=h).status_code == 403


def test_anonymous_rejected(client):
    assert client.get(URL).status_code in (401, 403)
