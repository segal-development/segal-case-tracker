"""Tests for GET /api/v1/calendar — slice 2a of req #5/#11 (calendarización).

Agenda of upcoming/overdue DEADLINES and REVIEW dates across the caller's
scoped cases, sorted by date ascending. Reuses data already denormalized on
``Case`` by ``DeadlineEngine``/``DecisionEngine``: ``next_deadline_at``,
``next_deadline_fatal``, ``next_review_at``, ``recommended_action_code``.
"""

import re
from contextlib import contextmanager

import pytest
from datetime import date, datetime, timedelta
from sqlalchemy import event

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer

TODAY = date(2026, 7, 9)


@contextmanager
def capture_selects(table: str):
    """Capture the SELECT statements issued against *table* while the block
    runs. The conftest engine is shared by ``db`` and ``client``, so a listener
    on it sees what the endpoint itself emits."""
    from tests.conftest import engine

    pattern = re.compile(r"\bFROM\s+" + re.escape(table) + r"\b", re.IGNORECASE)
    captured: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        stripped = statement.strip()
        if stripped[:6].upper() == "SELECT" and pattern.search(stripped):
            captured.append(stripped)

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield captured
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


@contextmanager
def count_selects(table: str):
    """Count SELECTs issued against *table* while the block runs.

    The conftest engine is shared by the ``db`` and ``client`` fixtures, so a
    listener on it sees the statements the endpoint itself emits.
    """
    from tests.conftest import engine

    pattern = re.compile(r"\bFROM\s+" + re.escape(table) + r"\b", re.IGNORECASE)
    counter = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        stripped = statement.strip()
        if stripped[:6].upper() == "SELECT" and pattern.search(stripped):
            counter["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """Pin the endpoint's clock to TODAY for deterministic window assertions."""
    monkeypatch.setattr("app.api.v1.calendar._today_chile", lambda: TODAY)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Cal Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut="55555555-5", name="Other Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut="77777777-7", name="Auditor User", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-CAL", name="Juzgado Calendario", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status=kwargs.pop("status", "active"),
        competencia="civil",
        plaintiff=kwargs.pop("plaintiff", "Banco X"),
        defendant=kwargs.pop("defendant", "Deudor Y"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigante(db, case, rut, nombre):
    db.add(CaseLitigante(
        case_id=case.id,
        participante="AB.DDO",
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case.id}-{rut}",
    ))
    db.commit()


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client
    app.dependency_overrides.pop(get_current_lawyer, None)


class TestQueryCost:
    """The sidebar calls this endpoint on every page load, so its cost is paid
    constantly and by every admin. Two shapes are pinned here:

    * the deadline label must NOT be resolved one query per case;
    * cases that can never produce an item — no ``next_deadline_at`` and no
      ``next_review_at`` — must not be loaded at all. On the real portfolio
      that is 11.959 of 14.645 rows hydrated to be discarded in Python.
    """

    def test_deadline_labels_resolve_in_one_query(self, authed_client, db, lawyer, court):
        for i in range(12):
            case = _make_case(
                db, lawyer, court, f"C-Q{i}-2025",
                next_deadline_at=TODAY + timedelta(days=3),
            )
            db.add(CaseDeadline(
                case_id=case.id,
                deadline_type="excepciones",
                due_date=TODAY + timedelta(days=3),
                triggered_at=TODAY,
                status="active",
            ))
        db.commit()

        with count_selects("case_deadlines") as counter:
            resp = authed_client.get("/api/v1/calendar?days=30")

        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 12
        assert counter["count"] <= 1, (
            f"one SELECT per case on case_deadlines: {counter['count']} for 12 cases"
        )

    def test_cases_without_dates_are_filtered_in_sql(self, authed_client, db, lawyer, court):
        """A causa with neither date can never produce an item, so it must be
        excluded by the query and not hydrated to be discarded in Python."""
        _make_case(db, lawyer, court, "C-CON-2025", next_review_at=TODAY + timedelta(days=5))
        for i in range(20):
            _make_case(db, lawyer, court, f"C-SIN-{i}-2025")

        with capture_selects("cases") as statements:
            resp = authed_client.get("/api/v1/calendar?days=30")

        assert resp.status_code == 200
        assert len(resp.json()["opportunities"]) == 1
        assert statements, "the endpoint issued no SELECT against cases"
        main = max(statements, key=len)
        assert "next_deadline_at IS NOT NULL" in main and "next_review_at IS NOT NULL" in main, (
            "the cases query does not filter out rows without any date:\n" + main
        )

    def test_court_name_is_not_lazy_loaded_per_case(self, authed_client, db, lawyer, court):
        # One court PER case: sharing a court would let SQLAlchemy's identity
        # map serve the lazy load from memory and hide the N+1 entirely.
        for i in range(10):
            own_court = Court(code=f"T-CT{i}", name=f"Juzgado {i}", region="RM", type="civil")
            db.add(own_court)
            db.commit()
            db.refresh(own_court)
            _make_case(
                db, lawyer, own_court, f"C-CT{i}-2025",
                next_review_at=TODAY + timedelta(days=4),
            )

        with count_selects("courts") as counter:
            resp = authed_client.get("/api/v1/calendar?days=30")

        assert resp.status_code == 200
        assert len(resp.json()["opportunities"]) == 10
        assert counter["count"] <= 1, (
            f"court loaded separately {counter['count']} times — expected a join"
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_returns_401_without_auth(self, client):
        response = client.get("/api/v1/calendar")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Horizon window
# ---------------------------------------------------------------------------


class TestHorizonWindow:
    def test_deadline_within_horizon_included(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1001-2026",
            next_deadline_at=TODAY + timedelta(days=5),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(i["case_id"] == c.id and i["kind"] == "deadline" for i in items)

    def test_deadline_beyond_horizon_excluded(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1002-2026",
            next_deadline_at=TODAY + timedelta(days=45),
            next_deadline_fatal=False,
            semaforo="verde",
        )
        response = authed_client.get("/api/v1/calendar?days=30")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_deadline_exactly_at_horizon_boundary_included(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1003-2026",
            next_deadline_at=TODAY + timedelta(days=30),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar?days=30")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(i["case_id"] == c.id for i in items)


# ---------------------------------------------------------------------------
# include_overdue
# ---------------------------------------------------------------------------


class TestIncludeOverdue:
    def test_include_overdue_false_excludes_past_due(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1101-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=false")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_include_overdue_true_includes_with_overdue_flag(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1102-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=true")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(i for i in items if i["case_id"] == c.id)
        assert item["overdue"] is True

    def test_include_overdue_true_but_verde_still_excluded(self, authed_client, db, lawyer, court):
        """A cleared/verde case's past-due deadline is not 'still active' —
        excluded even when include_overdue=true."""
        _make_case(
            db, lawyer, court, "C-1103-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=False,
            semaforo="verde",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=true")
        assert response.status_code == 200
        assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# Two items per case + recommended_action resolution
# ---------------------------------------------------------------------------


class TestDeadlineAndOpportunityPerCase:
    def test_case_with_plazo_and_distinct_review_date_splits_across_lists(
        self, authed_client, db, lawyer, court
    ):
        c = _make_case(
            db, lawyer, court, "C-1201-2026",
            next_deadline_at=TODAY + timedelta(days=2),
            next_deadline_fatal=True,
            next_review_at=TODAY + timedelta(days=4),
            recommended_action_code="oponer_excepciones",
            semaforo="rojo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert [i["kind"] for i in body["items"] if i["case_id"] == c.id] == ["deadline"]
        assert [o["case_id"] for o in body["opportunities"]] == [c.id]

    def test_opportunity_resolves_recommended_action_text_and_urgency(
        self, authed_client, db, lawyer, court
    ):
        c = _make_case(
            db, lawyer, court, "C-1202-2026",
            next_review_at=TODAY + timedelta(days=1),
            recommended_action_code="oponer_excepciones",
            semaforo="rojo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert body["items"] == []
        (opp,) = [o for o in body["opportunities"] if o["case_id"] == c.id]
        assert opp["recommended_action"] == "Oponer excepciones (escrito de oposición)"
        assert opp["urgency"] == "critica"

    def test_opportunity_with_unknown_code_resolves_to_none(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1203-2026",
            next_review_at=TODAY + timedelta(days=1),
            recommended_action_code="not_a_real_code",
            semaforo="amarillo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        opp = next(o for o in body["opportunities"] if o["case_id"] == c.id)
        assert opp["recommended_action"] is None
        assert opp["urgency"] is None


class TestMirroredReviewNotDuplicated:
    """``DecisionEngine`` mirrors the plazo into ``next_review_at`` whenever a
    case has one, so such a case must yield ONE item, not a deadline plus a
    redundant review on the same date (which doubled the Agenda KPIs)."""

    def test_case_with_deadline_yields_single_deadline_item(
        self, authed_client, db, lawyer, court
    ):
        due = TODAY + timedelta(days=2)
        c = _make_case(
            db, lawyer, court, "C-1401-2026",
            next_deadline_at=due,
            next_deadline_fatal=True,
            next_review_at=due,
            recommended_action_code="oponer_excepciones",
            semaforo="rojo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        items = [i for i in body["items"] if i["case_id"] == c.id]
        assert [i["kind"] for i in items] == ["deadline"]
        assert items[0]["fatal"] is True
        assert body["summary"]["total"] == 1

    def test_overdue_deadline_counts_once(self, authed_client, db, lawyer, court):
        due = TODAY - timedelta(days=3)
        _make_case(
            db, lawyer, court, "C-1402-2026",
            next_deadline_at=due,
            next_review_at=due,
            semaforo="rojo",
        )
        summary = authed_client.get("/api/v1/calendar").json()["summary"]
        assert summary["total"] == 1
        assert summary["overdue_count"] == 1

    def test_case_without_deadline_surfaces_as_opportunity(
        self, authed_client, db, lawyer, court
    ):
        c = _make_case(
            db, lawyer, court, "C-1403-2026",
            next_review_at=TODAY + timedelta(days=5),
            recommended_action_code="oponer_excepciones",
            semaforo="amarillo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert [i for i in body["items"] if i["case_id"] == c.id] == []
        assert [o["case_id"] for o in body["opportunities"]] == [c.id]

    def test_mixed_cases_are_each_counted_once(self, authed_client, db, lawyer, court):
        due = TODAY + timedelta(days=1)
        _make_case(
            db, lawyer, court, "C-1404-2026",
            next_deadline_at=due, next_deadline_fatal=True, next_review_at=due,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1405-2026",
            next_deadline_at=TODAY - timedelta(days=2), next_review_at=TODAY - timedelta(days=2),
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1406-2026",
            next_review_at=TODAY + timedelta(days=6),
            recommended_action_code="oponer_excepciones",
            semaforo="amarillo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert [i["kind"] for i in body["items"]] == ["deadline", "deadline"]
        assert len(body["opportunities"]) == 1
        assert body["summary"]["total"] == 2
        assert body["summary"]["overdue_count"] == 1

    def test_fatal_count_unchanged_by_dedup(self, authed_client, db, lawyer, court):
        due = TODAY + timedelta(days=1)
        _make_case(
            db, lawyer, court, "C-1407-2026",
            next_deadline_at=due, next_deadline_fatal=True, next_review_at=due,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1408-2026",
            next_deadline_at=due, next_deadline_fatal=False, next_review_at=due,
            semaforo="amarillo",
        )
        assert authed_client.get("/api/v1/calendar").json()["summary"]["fatal_count"] == 1


# ---------------------------------------------------------------------------
# Fatal flag + summary
# ---------------------------------------------------------------------------


class TestFatalAndSummary:
    def test_fatal_flag_on_deadline_item(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1301-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        item = next(i for i in response.json()["items"] if i["case_id"] == c.id)
        assert item["fatal"] is True

    def test_summary_counts(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1302-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1303-2026",
            next_deadline_at=TODAY - timedelta(days=2),
            next_deadline_fatal=False,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1304-2026",
            next_deadline_at=TODAY + timedelta(days=3),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        summary = response.json()["summary"]
        assert summary["total"] == 3
        assert summary["overdue_count"] == 1
        assert summary["fatal_count"] == 1


# ---------------------------------------------------------------------------
# Sorting + caratulado
# ---------------------------------------------------------------------------


class TestSortingAndCaratulado:
    def test_items_sorted_by_date_ascending(self, authed_client, db, lawyer, court):
        c_late = _make_case(
            db, lawyer, court, "C-1401-2026",
            next_deadline_at=TODAY + timedelta(days=10),
            semaforo="amarillo",
        )
        c_early = _make_case(
            db, lawyer, court, "C-1402-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        items = response.json()["items"]
        dates = [i["date"] for i in items]
        assert dates == sorted(dates)
        assert items[0]["case_id"] == c_early.id
        assert items[-1]["case_id"] == c_late.id

    def test_caratulado_reconstructed_from_plaintiff_defendant(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1403-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="amarillo",
            plaintiff="BANCO ACME",
            defendant="JUAN PEREZ",
        )
        response = authed_client.get("/api/v1/calendar")
        item = next(i for i in response.json()["items"] if i["case_id"] == c.id)
        assert item["caratulado"] == "BANCO ACME/JUAN PEREZ"
        assert item["rol"] == "C-1403-2026"
        assert item["court_name"] == court.name


# ---------------------------------------------------------------------------
# Deadline label resolution (Slice 2b, Task 3)
# ---------------------------------------------------------------------------


class TestDeadlineLabel:
    def test_deadline_item_shows_real_label_not_generic(self, authed_client, db, lawyer, court):
        """A deadline-kind calendar item resolves the real DeadlineType label
        (via the backing CaseDeadline row), reusing the same DEADLINE_LABELS
        catalog GET /cases/{id}/deadlines already uses — not a generic
        placeholder string."""
        c = _make_case(
            db, lawyer, court, "C-1701-2026",
            next_deadline_at=TODAY + timedelta(days=2),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        db.add(CaseDeadline(
            case_id=c.id,
            deadline_type="traslado_ejecutante_4d",
            legal_basis="art. 466 CPC",
            due_date=TODAY + timedelta(days=2),
            triggered_at=TODAY,
            status="active",
        ))
        db.commit()

        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        item = next(
            i for i in response.json()["items"]
            if i["case_id"] == c.id and i["kind"] == "deadline"
        )
        assert item["label"] == "Traslado al ejecutante"
        assert item["label"] != "Plazo procesal"

    def test_deadline_item_without_backing_row_has_none_label(self, authed_client, db, lawyer, court):
        """No matching active CaseDeadline row → label is None (safe fallback),
        not a crash — e.g. a next_deadline_at written without a backing row."""
        c = _make_case(
            db, lawyer, court, "C-1702-2026",
            next_deadline_at=TODAY + timedelta(days=2),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        item = next(
            i for i in response.json()["items"]
            if i["case_id"] == c.id and i["kind"] == "deadline"
        )
        assert item["label"] is None


# ---------------------------------------------------------------------------
# Archived cases excluded
# ---------------------------------------------------------------------------


class TestArchivedExcluded:
    def test_archived_case_excluded(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1501-2026",
            status="archived",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


class TestScoping:
    def test_regular_lawyer_sees_only_own_cases(
        self, client, db, lawyer, other_lawyer, court
    ):
        from app.core.security import create_access_token

        own_case = _make_case(
            db, lawyer, court, "C-1601-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, own_case, lawyer.rut, lawyer.name)

        other_case = _make_case(
            db, other_lawyer, court, "C-1602-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, other_case, other_lawyer.rut, other_lawyer.name)

        token = create_access_token({"sub": lawyer.rut}, expires_delta=timedelta(minutes=30))
        response = client.get(
            "/api/v1/calendar", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        case_ids = {i["case_id"] for i in response.json()["items"]}
        assert own_case.id in case_ids
        assert other_case.id not in case_ids

    def test_auditor_sees_cases_from_all_lawyers(
        self, client, db, lawyer, other_lawyer, auditor, court
    ):
        from app.core.security import create_access_token

        case_a = _make_case(
            db, lawyer, court, "C-1603-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, case_a, lawyer.rut, lawyer.name)

        case_b = _make_case(
            db, other_lawyer, court, "C-1604-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, case_b, other_lawyer.rut, other_lawyer.name)

        token = create_access_token({"sub": auditor.rut}, expires_delta=timedelta(minutes=30))
        response = client.get(
            "/api/v1/calendar", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        case_ids = {i["case_id"] for i in response.json()["items"]}
        assert case_a.id in case_ids
        assert case_b.id in case_ids


class TestAbogadoRutScoping:
    """abogado_rut narrows an auditor/admin's calendar to one lawyer's cases."""

    @pytest.fixture
    def auditor_client(self, client, auditor):
        async def _mock():
            return {"sub": str(auditor.id)}

        app.dependency_overrides[get_current_lawyer] = _mock
        yield client
        app.dependency_overrides.pop(get_current_lawyer, None)

    def _two_cases(self, db, lawyer, court):
        from datetime import timedelta

        # Use the frozen TODAY (not the real date.today()) so the seeded deadline
        # stays inside the endpoint's [TODAY, TODAY+30] window regardless of the
        # calendar date — else the test fails once real-today drifts past TODAY+27.
        due = TODAY + timedelta(days=3)
        mine = _make_case(db, lawyer, court, "C-1-2026",
                          next_deadline_at=due, next_deadline_fatal=True, semaforo="rojo")
        _seed_litigante(db, mine, "99999999-9", "Abogada Carla")
        theirs = _make_case(db, lawyer, court, "C-2-2026",
                            next_deadline_at=due, semaforo="rojo")
        _seed_litigante(db, theirs, "44444444-4", "Otro Abogado")
        return mine, theirs

    def test_without_param_auditor_sees_both(self, auditor_client, db, lawyer, court):
        self._two_cases(db, lawyer, court)
        resp = auditor_client.get("/api/v1/calendar?days=30")
        assert resp.status_code == 200
        rols = {i["rol"] for i in resp.json()["items"]}
        assert {"C-1-2026", "C-2-2026"} <= rols

    def test_abogado_rut_narrows_to_that_lawyers_cases(self, auditor_client, db, lawyer, court):
        self._two_cases(db, lawyer, court)
        resp = auditor_client.get("/api/v1/calendar?days=30&abogado_rut=99999999-9")
        assert resp.status_code == 200
        rols = {i["rol"] for i in resp.json()["items"]}
        assert "C-1-2026" in rols
        assert "C-2-2026" not in rols


# ---------------------------------------------------------------------------
# Opportunities: undated, returned apart from the dated items
# ---------------------------------------------------------------------------


class TestOpportunities:
    """A standing opportunity (abandono / prescripción / apremio) has no legal
    date. The ``next_review_at`` the engine stores for it is derived from the
    case id purely to spread review load, so the Agenda must not present it as
    a date next to real plazos."""

    def test_opportunity_without_plazo_is_not_a_dated_item(
        self, authed_client, db, lawyer, court
    ):
        c = _make_case(
            db, lawyer, court, "C-2001-2026",
            next_review_at=TODAY + timedelta(days=9),
            recommended_action_code="oponer_excepciones",
            semaforo="amarillo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert body["items"] == []
        (opp,) = body["opportunities"]
        assert opp["case_id"] == c.id
        assert opp["rol"] == "C-2001-2026"
        assert opp["caratulado"] == "Banco X/Deudor Y"
        assert opp["court_name"] == "Juzgado Calendario"
        assert opp["semaforo"] == "amarillo"

    def test_opportunity_exposes_no_date_field(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-2002-2026",
            next_review_at=TODAY + timedelta(days=9),
            recommended_action_code="oponer_excepciones",
            semaforo="amarillo",
        )
        (opp,) = authed_client.get("/api/v1/calendar").json()["opportunities"]
        assert set(opp) == {
            "case_id", "rol", "caratulado", "court_name",
            "semaforo", "recommended_action", "urgency",
        }
        assert "date" not in opp and "overdue" not in opp

    def test_summary_total_counts_only_dated_items(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-2003-2026",
            next_deadline_at=TODAY + timedelta(days=2), semaforo="rojo",
        )
        for i in range(3):
            _make_case(
                db, lawyer, court, f"C-2004-{i}-2026",
                next_review_at=TODAY + timedelta(days=8 + i),
                recommended_action_code="oponer_excepciones",
                semaforo="amarillo",
            )
        summary = authed_client.get("/api/v1/calendar").json()["summary"]
        assert summary["total"] == 1
        assert summary["opportunities_count"] == 3

    def test_opportunities_count_matches_list(self, authed_client, db, lawyer, court):
        for i in range(2):
            _make_case(
                db, lawyer, court, f"C-2005-{i}-2026",
                next_review_at=TODAY + timedelta(days=8),
                semaforo="amarillo",
            )
        body = authed_client.get("/api/v1/calendar").json()
        assert body["summary"]["opportunities_count"] == len(body["opportunities"]) == 2

    def test_opportunities_ordered_by_urgency_then_rol_never_by_date(
        self, authed_client, db, lawyer, court
    ):
        # Dates run opposite to the expected order on purpose: if the synthetic
        # date leaked into the sort, this would fail.
        from app.core.decision_rules import DECISION_RULES

        by_urgency = {}
        for rule in DECISION_RULES:
            by_urgency.setdefault(rule.urgency.value, rule.code)
        assert {"critica", "alta"} <= set(by_urgency), "need two urgency tiers in the catalog"

        specs = [
            ("C-9-2026", "alta", 1),
            ("C-3-2026", "critica", 20),
            ("C-1-2026", "alta", 30),
            ("C-2-2026", "critica", 10),
        ]
        for rol, urgency, offset in specs:
            _make_case(
                db, lawyer, court, rol,
                next_review_at=TODAY + timedelta(days=offset),
                recommended_action_code=by_urgency[urgency],
                semaforo="amarillo",
            )
        opps = authed_client.get("/api/v1/calendar").json()["opportunities"]
        assert [(o["urgency"], o["rol"]) for o in opps] == [
            ("critica", "C-2-2026"),
            ("critica", "C-3-2026"),
            ("alta", "C-1-2026"),
            ("alta", "C-9-2026"),
        ]

    def test_unknown_urgency_sorts_last(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-0-2026",
            next_review_at=TODAY + timedelta(days=8),
            recommended_action_code="not_a_real_code", semaforo="amarillo",
        )
        _make_case(
            db, lawyer, court, "C-5-2026",
            next_review_at=TODAY + timedelta(days=8),
            recommended_action_code="oponer_excepciones", semaforo="amarillo",
        )
        opps = authed_client.get("/api/v1/calendar").json()["opportunities"]
        assert [o["rol"] for o in opps] == ["C-5-2026", "C-0-2026"]

    def test_opportunities_ignore_window_and_semaforo(self, authed_client, db, lawyer, court):
        """No date means no window: a far-future or stale-past synthetic date,
        or ``days=1``/``include_overdue=false``, must not hide an opportunity."""
        _make_case(
            db, lawyer, court, "C-2101-2026",
            next_review_at=TODAY + timedelta(days=37), semaforo="amarillo",
        )
        _make_case(
            db, lawyer, court, "C-2102-2026",
            next_review_at=TODAY - timedelta(days=40), semaforo="verde",
        )
        resp = authed_client.get("/api/v1/calendar?days=1&include_overdue=false")
        body = resp.json()
        assert sorted(o["rol"] for o in body["opportunities"]) == ["C-2101-2026", "C-2102-2026"]
        assert body["summary"]["opportunities_count"] == 2

    def test_case_with_plazo_stays_a_deadline_item(self, authed_client, db, lawyer, court):
        due = TODAY + timedelta(days=3)
        c = _make_case(
            db, lawyer, court, "C-2201-2026",
            next_deadline_at=due, next_deadline_fatal=True, next_review_at=due,
            recommended_action_code="oponer_excepciones", semaforo="rojo",
        )
        db.add(CaseDeadline(
            case_id=c.id, deadline_type="excepciones", due_date=due,
            triggered_at=TODAY, status="active",
        ))
        db.commit()
        body = authed_client.get("/api/v1/calendar").json()
        (item,) = body["items"]
        assert item["kind"] == "deadline"
        assert item["date"] == due.isoformat()
        assert item["fatal"] is True
        assert item["overdue"] is False
        assert body["opportunities"] == []
        assert body["summary"] == {
            "total": 1, "overdue_count": 0, "fatal_count": 1, "opportunities_count": 0,
        }

    def test_opportunities_respect_scope(self, authed_client, db, lawyer, other_lawyer, court):
        _make_case(
            db, other_lawyer, court, "C-2301-2026",
            next_review_at=TODAY + timedelta(days=8), semaforo="amarillo",
        )
        body = authed_client.get("/api/v1/calendar").json()
        assert body["opportunities"] == []

    def test_archived_case_is_not_an_opportunity(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-2302-2026", status="archived",
            next_review_at=TODAY + timedelta(days=8), semaforo="amarillo",
        )
        assert authed_client.get("/api/v1/calendar").json()["opportunities"] == []
