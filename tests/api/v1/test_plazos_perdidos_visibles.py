"""A lost fatal plazo (``Case.plazo_fatal_incumplido``) must stay countable.

The engine nulls ``next_deadline_at`` when a fatal plazo is lost, so every
counter that reads the date silently drops the case. The risk board and the
calendar summary expose it as its OWN indicator, never mixed into "vencidos"
or "fatales" (the semaforo measures pending action, compliance is another axis).
"""

import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.deadline_engine import _today_chile
from app.services.lawyer_roster import firm_risk_board

TODAY = date(2026, 7, 9)
FIRM_RUT = "11111111-1"
OTHER_RUT = "22222222-2"


@pytest.fixture(autouse=True)
def _freeze_calendar_today(monkeypatch):
    monkeypatch.setattr("app.api.v1.calendar._today_chile", lambda: TODAY)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=FIRM_RUT, name="Firm Lawyer", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut=OTHER_RUT, name="Other Lawyer", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut="77777777-7", name="Auditor", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-PPV", name="Juzgado PPV", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _case(db, owner, court, rol, *, abogado=None, **kwargs):
    obj = Case(
        lawyer_id=owner.id,
        court_id=court.id,
        rol=rol,
        status=kwargs.pop("status", "active"),
        competencia="civil",
        plaintiff="Banco X",
        defendant="Deudor Y",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    if abogado is not None:
        db.add(CaseLitigante(
            case_id=obj.id,
            participante="AB.DDO",
            rut=abogado.rut,
            persona_type="NATURAL",
            nombre=abogado.name,
            natural_key=f"{obj.id}-{abogado.rut}",
        ))
        db.commit()
    return obj


@contextmanager
def count_selects(table: str):
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


def _client_as(client, who):
    async def _mock():
        return {"sub": str(who.id)}

    app.dependency_overrides[get_current_lawyer] = _mock


@pytest.fixture(autouse=True)
def _clear_override():
    yield
    app.dependency_overrides.pop(get_current_lawyer, None)


# ---------------------------------------------------------------------------
# Risk board
# ---------------------------------------------------------------------------


class TestRiskBoardIncumplido:
    def test_flagged_case_counted_and_not_in_vencido(self, db, lawyer, court):
        _case(db, lawyer, court, "C-1-2026", abogado=lawyer, semaforo="verde",
              plazo_fatal_incumplido=True, next_deadline_at=None)
        riesgo = firm_risk_board(db, FIRM_RUT)["riesgo"]
        assert riesgo["plazo_fatal_incumplido"] == 1
        assert riesgo["plazo_fatal_vencido"] == 0
        assert riesgo["plazo_fatal_proximo"] == 0

    def test_unflagged_cases_not_counted(self, db, lawyer, court):
        _case(db, lawyer, court, "C-2-2026", abogado=lawyer, semaforo="verde")
        _case(db, lawyer, court, "C-3-2026", abogado=lawyer, semaforo="rojo",
              next_deadline_fatal=True, next_deadline_at=_today_chile() - timedelta(days=2))
        riesgo = firm_risk_board(db, FIRM_RUT)["riesgo"]
        assert riesgo["plazo_fatal_incumplido"] == 0
        assert riesgo["plazo_fatal_vencido"] == 1

    def test_universe_rule_respected(self, db, lawyer, court):
        # No firm abogado of record -> outside the board's universe.
        _case(db, lawyer, court, "C-4-2026", plazo_fatal_incumplido=True)
        # Archived -> outside the universe even with an abogado.
        _case(db, lawyer, court, "C-5-2026", abogado=lawyer, status="archived",
              plazo_fatal_incumplido=True)
        assert firm_risk_board(db, FIRM_RUT)["riesgo"]["plazo_fatal_incumplido"] == 0

    def test_top_critical_puts_flagged_case_first(self, db, lawyer, court):
        today = _today_chile()
        _case(db, lawyer, court, "C-6-2026", abogado=lawyer, semaforo="rojo",
              next_deadline_fatal=True, next_deadline_at=today + timedelta(days=1))
        flagged = _case(db, lawyer, court, "C-7-2026", abogado=lawyer, semaforo="verde",
                        plazo_fatal_incumplido=True)
        top = firm_risk_board(db, FIRM_RUT)["top_critical"]
        assert top[0]["case_id"] == flagged.id

    def test_totals_unchanged_without_flagged_cases(self, db, lawyer, court):
        today = _today_chile()
        _case(db, lawyer, court, "C-8-2026", abogado=lawyer, semaforo="rojo",
              abandono_disponible=True, next_deadline_fatal=True,
              next_deadline_at=today + timedelta(days=3))
        _case(db, lawyer, court, "C-9-2026", abogado=lawyer, semaforo="amarillo",
              en_apremio=True, next_deadline_fatal=True,
              next_deadline_at=today - timedelta(days=3))
        board = firm_risk_board(db, FIRM_RUT)
        assert board["riesgo"] == {
            "abandono_disponible": 1,
            "prescripcion_cumplida": 0,
            "en_apremio": 1,
            "plazo_fatal_proximo": 1,
            "plazo_fatal_vencido": 1,
            "plazo_fatal_incumplido": 0,
        }
        assert [c["rol"] for c in board["top_critical"]] == ["C-8-2026", "C-9-2026"]

    def test_endpoint_exposes_key(self, client, db, auditor, lawyer, court):
        from app.core.security import create_access_token

        _case(db, lawyer, court, "C-10-2026", abogado=lawyer, semaforo="verde",
              plazo_fatal_incumplido=True)
        token = create_access_token({"sub": auditor.rut, "role": "auditor"})
        resp = client.get(
            "/api/v1/stats/risk-board", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["riesgo"]["plazo_fatal_incumplido"] == 1


# ---------------------------------------------------------------------------
# Calendar summary
# ---------------------------------------------------------------------------


class TestCalendarIncumplido:
    def test_flagged_case_counted_apart_from_dated_counters(
        self, client, db, lawyer, court
    ):
        flagged = _case(db, lawyer, court, "C-20-2026", abogado=lawyer, semaforo="verde",
                        plazo_fatal_incumplido=True, next_deadline_at=None)
        _client_as(client, lawyer)
        body = client.get("/api/v1/calendar?days=30").json()
        assert body["summary"] == {
            "total": 0,
            "overdue_count": 0,
            "fatal_count": 0,
            "opportunities_count": 0,
            "incumplido_count": 1,
        }
        assert body["items"] == []
        assert all(o["case_id"] != flagged.id for o in body["opportunities"])

    def test_unflagged_cases_not_counted(self, client, db, lawyer, court):
        _case(db, lawyer, court, "C-21-2026", abogado=lawyer,
              next_deadline_at=TODAY - timedelta(days=2), next_deadline_fatal=True)
        _client_as(client, lawyer)
        summary = client.get("/api/v1/calendar").json()["summary"]
        assert summary["incumplido_count"] == 0
        assert summary["overdue_count"] == 1
        assert summary["fatal_count"] == 1

    def test_archived_flagged_case_not_counted(self, client, db, lawyer, court):
        _case(db, lawyer, court, "C-22-2026", abogado=lawyer, status="archived",
              plazo_fatal_incumplido=True)
        _client_as(client, lawyer)
        assert client.get("/api/v1/calendar").json()["summary"]["incumplido_count"] == 0

    def test_scope_hides_case_from_lawyer_not_on_it(
        self, client, db, lawyer, other_lawyer, court
    ):
        _case(db, lawyer, court, "C-23-2026", abogado=lawyer, plazo_fatal_incumplido=True)
        _client_as(client, other_lawyer)
        assert client.get("/api/v1/calendar").json()["summary"]["incumplido_count"] == 0
        _client_as(client, lawyer)
        assert client.get("/api/v1/calendar").json()["summary"]["incumplido_count"] == 1

    def test_auditor_sees_all_and_abogado_rut_narrows(
        self, client, db, auditor, lawyer, other_lawyer, court
    ):
        _case(db, lawyer, court, "C-24-2026", abogado=lawyer, plazo_fatal_incumplido=True)
        _case(db, other_lawyer, court, "C-25-2026", abogado=other_lawyer,
              plazo_fatal_incumplido=True)
        _client_as(client, auditor)
        assert client.get("/api/v1/calendar").json()["summary"]["incumplido_count"] == 2
        narrowed = client.get(f"/api/v1/calendar?abogado_rut={OTHER_RUT}").json()
        assert narrowed["summary"]["incumplido_count"] == 1

    def test_adds_at_most_one_statement(self, client, db, lawyer, court):
        _case(db, lawyer, court, "C-26-2026", abogado=lawyer,
              next_deadline_at=TODAY + timedelta(days=2))
        _client_as(client, lawyer)
        with count_selects("cases") as baseline:
            client.get("/api/v1/calendar")
        _case(db, lawyer, court, "C-27-2026", abogado=lawyer, plazo_fatal_incumplido=True)
        with count_selects("cases") as with_flagged:
            body = client.get("/api/v1/calendar").json()
        assert body["summary"]["incumplido_count"] == 1
        assert with_flagged["count"] <= baseline["count"] + 1
