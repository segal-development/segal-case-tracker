# ruff: noqa: F811
"""Tests for GET /api/v1/alerts?group_by=case_day and POST /api/v1/alerts/read-group.

Groups are keyed by (case_id, Chile calendar day of created_at). Chile is
UTC-3 in summer (DST) and UTC-4 in winter; `Alert.created_at` is naive UTC.
"""

from datetime import datetime, timedelta

import pytest

from app.models.alert import Alert
from app.models.case import Case
from tests.api.v1.test_alerts import (  # noqa: F401  (fixtures + helpers)
    CARLA_RUT,
    SANDY_RUT,
    _headers,
    _make_alert,
    carla,
    case,
    court,
    sandy,
)

URL = "/api/v1/alerts"


def _other_case(db, court, rol="C-9002-2026"):
    obj = Case(
        lawyer_id=1,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff="OTRO",
        defendant="OTRA",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _get(client, rut=SANDY_RUT, qs="category=activity"):
    resp = client.get(f"{URL}?group_by=case_day&{qs}", headers=_headers(rut))
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestDefaultResponseUnchanged:
    def test_flat_response_is_byte_identical_without_group_by(self, client, db, sandy, case):
        a = _make_alert(
            db, sandy.id, case.id, title="Mov", created_at=datetime(2026, 1, 15, 12, 0, 0)
        )
        resp = client.get(f"{URL}?category=all", headers=_headers(SANDY_RUT))
        expected = (
            '{"items":[{"id":@A@,"type":"new_movement","title":"Mov","message":"msg",'
            '"case_id":@C@,"created_at":"2026-01-15T12:00:00","read":false,"read_at":null,'
            '"case_rol":"C-9001-2026","case_caratulado":"DEMANDANTE/DEMANDADO",'
            '"case_court_name":"Juzgado Alerts"}],"total":1,"page":1,"per_page":20,'
            '"pages":1,"unread_count":1}'
        )
        expected = expected.replace("@A@", str(a.id)).replace("@C@", str(case.id))
        assert resp.content.decode() == expected

    def test_explicit_group_by_none_value_rejected(self, client, sandy):
        resp = client.get(f"{URL}?group_by=bogus", headers=_headers(SANDY_RUT))
        assert resp.status_code == 422


class TestGroupShape:
    def test_group_fields(self, client, db, sandy, case):
        t0 = datetime(2026, 1, 15, 15, 0, 0)  # 12:00 Chile, 2026-01-15
        first = _make_alert(db, sandy.id, case.id, title="First", created_at=t0, read=True)
        last = _make_alert(
            db, sandy.id, case.id, title="Last", created_at=t0 + timedelta(hours=1),
            type="new_notificacion",
        )
        body = _get(client)
        assert body["total"] == 1
        assert body["unread_count"] == 1
        assert body["page"] == 1 and body["per_page"] == 20 and body["pages"] == 1
        (g,) = body["items"]
        assert g == {
            "group_key": f"case:{case.id}:2026-01-15",
            "case_id": case.id,
            "rol": "C-9001-2026",
            "caratula": "DEMANDANTE/DEMANDADO",
            "day": "2026-01-15",
            "count": 2,
            "unread_count": 1,
            "latest_created_at": "2026-01-15T16:00:00",
            "latest_title": "Last",
            "latest_message": "msg",
            "types": ["new_movement", "new_notificacion"],
            "alert_ids": [last.id, first.id],
        }

    def test_groups_split_by_case_and_ordered_by_latest_desc(self, client, db, sandy, court, case):
        other = _other_case(db, court)
        base = datetime(2026, 1, 15, 15, 0, 0)
        _make_alert(db, sandy.id, case.id, created_at=base)
        _make_alert(db, sandy.id, other.id, created_at=base + timedelta(minutes=5))
        body = _get(client)
        assert [g["case_id"] for g in body["items"]] == [other.id, case.id]
        assert body["total"] == 2

    def test_alert_ids_capped_at_200(self, client, db, sandy, case):
        base = datetime(2026, 1, 15, 15, 0, 0)
        db.add_all(
            Alert(
                lawyer_id=sandy.id, case_id=case.id, type="new_movement", title=f"M{i}",
                message="m", read=False, created_at=base + timedelta(seconds=i),
            )
            for i in range(205)
        )
        db.commit()
        (g,) = _get(client)["items"]
        assert g["count"] == 205
        assert len(g["alert_ids"]) == 200
        assert g["latest_title"] == "M204"


class TestChileDayBoundary:
    @pytest.mark.parametrize(
        "utc, day",
        [
            # Summer (UTC-3): Chile midnight is 03:00 UTC.
            (datetime(2026, 1, 15, 2, 59, 59), "2026-01-14"),
            (datetime(2026, 1, 15, 3, 0, 0), "2026-01-15"),
            # Winter (UTC-4): Chile midnight is 04:00 UTC.
            (datetime(2026, 7, 15, 3, 59, 59), "2026-07-14"),
            (datetime(2026, 7, 15, 4, 0, 0), "2026-07-15"),
            # Plain UTC midnight is still the previous Chile day.
            (datetime(2026, 7, 15, 0, 0, 0), "2026-07-14"),
        ],
    )
    def test_day_uses_chile_calendar(self, client, db, sandy, case, utc, day):
        _make_alert(db, sandy.id, case.id, created_at=utc)
        (g,) = _get(client)["items"]
        assert g["day"] == day
        assert g["group_key"] == f"case:{case.id}:{day}"

    def test_same_utc_day_splits_in_two_chile_days(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, created_at=datetime(2026, 1, 15, 2, 0, 0))
        _make_alert(db, sandy.id, case.id, created_at=datetime(2026, 1, 15, 4, 0, 0))
        body = _get(client)
        assert body["total"] == 2
        assert [g["day"] for g in body["items"]] == ["2026-01-15", "2026-01-14"]


class TestGroupCountsAndFilters:
    def test_unread_count_is_groups_with_unread(self, client, db, sandy, court, case):
        other = _other_case(db, court)
        t = datetime(2026, 1, 15, 15, 0, 0)
        _make_alert(db, sandy.id, case.id, created_at=t, read=True)
        _make_alert(db, sandy.id, case.id, created_at=t, read=False)
        _make_alert(db, sandy.id, case.id, created_at=t, read=False)
        _make_alert(db, sandy.id, other.id, created_at=t, read=True)
        body = _get(client)
        assert body["total"] == 2
        assert body["unread_count"] == 1
        by_case = {g["case_id"]: g for g in body["items"]}
        assert by_case[case.id]["unread_count"] == 2
        assert by_case[other.id]["unread_count"] == 0

    def test_unread_only_keeps_groups_with_unread_and_full_counts(
        self, client, db, sandy, court, case
    ):
        other = _other_case(db, court)
        t = datetime(2026, 1, 15, 15, 0, 0)
        _make_alert(db, sandy.id, case.id, created_at=t, read=True)
        _make_alert(db, sandy.id, case.id, created_at=t, read=False)
        _make_alert(db, sandy.id, other.id, created_at=t, read=True)
        body = _get(client, qs="category=activity&unread_only=true")
        assert body["total"] == 1
        assert body["unread_count"] == 1
        assert body["items"][0]["case_id"] == case.id
        assert body["items"][0]["count"] == 2

    def test_only_callers_alerts(self, client, db, sandy, carla, case):
        t = datetime(2026, 1, 15, 15, 0, 0)
        _make_alert(db, sandy.id, case.id, created_at=t)
        _make_alert(db, carla.id, case.id, created_at=t)
        _make_alert(db, carla.id, case.id, created_at=t)
        body = _get(client)
        assert body["total"] == 1
        assert body["items"][0]["count"] == 1

    def test_category_activity_excludes_actionable_and_vice_versa(self, client, db, sandy, case):
        t = datetime(2026, 1, 15, 15, 0, 0)
        _make_alert(db, sandy.id, case.id, created_at=t, type="new_movement")
        _make_alert(db, sandy.id, case.id, created_at=t, type="semaforo_rojo")
        act = _get(client, qs="category=activity")["items"]
        assert act[0]["types"] == ["new_movement"] and act[0]["count"] == 1
        req = _get(client, qs="category=actionable")["items"]
        assert req[0]["types"] == ["semaforo_rojo"] and req[0]["count"] == 1
        every = _get(client, qs="category=all")["items"]
        assert every[0]["count"] == 2

    def test_actionable_keeps_stale_deadline_rule(self, client, db, court, sandy):
        stale = Case(
            lawyer_id=1, court_id=court.id, rol="C-1-2020", status="active",
            competencia="civil", next_deadline_at=datetime.utcnow() - timedelta(days=90),
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        )
        db.add(stale)
        db.commit()
        _make_alert(db, sandy.id, stale.id, type="deadline_fatal")
        _make_alert(db, sandy.id, stale.id, type="status_change")
        flat = client.get(f"{URL}?category=actionable", headers=_headers(SANDY_RUT)).json()
        body = _get(client, qs="category=actionable")
        assert flat["total"] == 1
        assert body["items"][0]["count"] == 1
        assert body["items"][0]["types"] == ["status_change"]

    def test_default_category_still_actionable(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, type="new_movement")
        resp = client.get(f"{URL}?group_by=case_day", headers=_headers(SANDY_RUT))
        assert resp.json()["total"] == 0


class TestGroupPagination:
    def test_pages_apply_to_groups(self, client, db, sandy, case):
        base = datetime(2026, 1, 1, 15, 0, 0)
        for d in range(5):
            for _ in range(3):
                _make_alert(db, sandy.id, case.id, created_at=base + timedelta(days=d))
        p1 = _get(client, qs="category=activity&per_page=2&page=1")
        p3 = _get(client, qs="category=activity&per_page=2&page=3")
        assert p1["total"] == 5 and p1["pages"] == 3 and len(p1["items"]) == 2
        assert [g["day"] for g in p1["items"]] == ["2026-01-05", "2026-01-04"]
        assert [g["day"] for g in p3["items"]] == ["2026-01-01"]
        assert p3["page"] == 3 and p3["per_page"] == 2

    def test_empty(self, client, sandy):
        body = _get(client)
        assert body == {
            "items": [], "total": 0, "page": 1, "per_page": 20, "pages": 0, "unread_count": 0,
        }


class TestReadGroup:
    def test_marks_group_alerts_only(self, client, db, sandy, carla, court, case):
        other = _other_case(db, court)
        t = datetime(2026, 1, 15, 15, 0, 0)
        a1 = _make_alert(db, sandy.id, case.id, created_at=t)
        a2 = _make_alert(db, sandy.id, case.id, created_at=t + timedelta(hours=1))
        next_day = _make_alert(db, sandy.id, case.id, created_at=t + timedelta(days=1))
        other_case = _make_alert(db, sandy.id, other.id, created_at=t)
        actionable = _make_alert(db, sandy.id, case.id, created_at=t, type="semaforo_rojo")
        carlas = _make_alert(db, carla.id, case.id, created_at=t)

        resp = client.post(
            f"{URL}/read-group",
            json={"group_key": f"case:{case.id}:2026-01-15", "category": "activity"},
            headers=_headers(SANDY_RUT),
        )
        assert resp.status_code == 200
        assert resp.json() == {"marked": 2}
        for obj in (a1, a2, next_day, other_case, actionable, carlas):
            db.refresh(obj)
        assert (a1.read, a2.read) == (True, True)
        assert a1.read_at is not None
        assert not any(o.read for o in (next_day, other_case, actionable, carlas))

    def test_default_category_is_activity(self, client, db, sandy, case):
        t = datetime(2026, 1, 15, 15, 0, 0)
        act = _make_alert(db, sandy.id, case.id, created_at=t)
        req = _make_alert(db, sandy.id, case.id, created_at=t, type="semaforo_rojo")
        resp = client.post(
            f"{URL}/read-group",
            json={"group_key": f"case:{case.id}:2026-01-15"},
            headers=_headers(SANDY_RUT),
        )
        assert resp.json() == {"marked": 1}
        db.refresh(act)
        db.refresh(req)
        assert act.read is True and req.read is False

    def test_uses_chile_day(self, client, db, sandy, case):
        late = _make_alert(db, sandy.id, case.id, created_at=datetime(2026, 1, 15, 2, 30, 0))
        early = _make_alert(db, sandy.id, case.id, created_at=datetime(2026, 1, 15, 3, 30, 0))
        client.post(
            f"{URL}/read-group",
            json={"group_key": f"case:{case.id}:2026-01-14"},
            headers=_headers(SANDY_RUT),
        )
        db.refresh(late)
        db.refresh(early)
        assert late.read is True and early.read is False

    def test_idempotent_and_updates_list(self, client, db, sandy, case):
        _make_alert(db, sandy.id, case.id, created_at=datetime(2026, 1, 15, 15, 0, 0))
        body = {"group_key": f"case:{case.id}:2026-01-15"}
        assert client.post(f"{URL}/read-group", json=body, headers=_headers(SANDY_RUT)).json() == {"marked": 1}
        assert client.post(f"{URL}/read-group", json=body, headers=_headers(SANDY_RUT)).json() == {"marked": 0}
        assert _get(client)["unread_count"] == 0

    def test_never_touches_other_lawyers_alerts(self, client, db, sandy, carla, case):
        theirs = _make_alert(db, carla.id, case.id, created_at=datetime(2026, 1, 15, 15, 0, 0))
        resp = client.post(
            f"{URL}/read-group",
            json={"group_key": f"case:{case.id}:2026-01-15"},
            headers=_headers(SANDY_RUT),
        )
        assert resp.json() == {"marked": 0}
        db.refresh(theirs)
        assert theirs.read is False

    @pytest.mark.parametrize("key", ["", "nope", "case:x:2026-01-15", "case:1:2026-13-45", "case:1", "other:1:2026-01-15"])
    def test_malformed_key_is_422(self, client, sandy, key):
        resp = client.post(f"{URL}/read-group", json={"group_key": key}, headers=_headers(SANDY_RUT))
        assert resp.status_code == 422

    def test_existing_read_endpoints_unaffected(self, client, db, sandy, case):
        a = _make_alert(db, sandy.id, case.id)
        assert client.post(f"{URL}/{a.id}/read", headers=_headers(SANDY_RUT)).status_code == 200
        assert client.post(f"{URL}/read-all", headers=_headers(SANDY_RUT)).json() == {"marked": 0}
