"""GET /cases/deadlines/semaforo -- the fatal-plazo panel for the home screen.

Calendar: trigger Monday 2026-11-02. Day 3 = Thu 11-05 (amarillo on the 4-day
reading), day 8 = Wed 11-11.
"""

from datetime import date, timedelta

import pytest
from uuid import uuid4

from app.config import settings
from app.core.security import create_access_token
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.business_days import add_business_days

URL = "/api/v1/cases/deadlines/semaforo"
TRIGGER = date(2026, 11, 2)
DAY3 = date(2026, 11, 5)
RUT = "16021492-9"
OTHER_RUT = "33333333-3"
ADMIN_RUT = "22222222-2"


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.api.v1.deadlines._today_chile", lambda: DAY3)


@pytest.fixture
def people(db):
    ana = Lawyer(rut=RUT, name="Ana Pérez", email="ana@segal.cl", role="lawyer", is_active=True)
    otro = Lawyer(rut=OTHER_RUT, name="Otro Abogado", email="o@segal.cl", role="lawyer", is_active=True)
    admin = Lawyer(rut=ADMIN_RUT, name="Carla Lavín", email="c@segal.cl", role="admin", is_active=True)
    court = Court(code="SF-1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add_all([ana, otro, admin, court])
    db.commit()
    return {"ana": ana, "otro": otro, "admin": admin, "court": court}


def make_case(db, people, owner="ana", rol="C-1-2026", status="active"):
    case = Case(
        lawyer_id=people[owner].id, court_id=people["court"].id, rol=rol,
        competencia="civil", plaintiff="Banco X", defendant="Deudor", status=status,
    )
    db.add(case)
    db.commit()
    return case


def make_live(db, case, type_="excepciones_8d", triggered=TRIGGER, **kw):
    days = {"excepciones_8d": 8, "acreditar_poder_3d": 3, "apelacion_5d": 10}[type_]
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, triggered_at=triggered,
        due_date=add_business_days(triggered, days), status="active", **kw,
    )
    db.add(row)
    db.commit()
    return row


def make_expired(db, case, type_="excepciones_8d", due=date(2026, 10, 20), verdict="no_cumplido"):
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, triggered_at=due - timedelta(days=13),
        due_date=due, status="superseded", verdict=verdict,
    )
    db.add(row)
    db.commit()
    return row


def get(client, rut=RUT):
    resp = client.get(URL, headers=_headers(rut))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_requires_authentication(client) -> None:
    assert client.get(URL).status_code in (401, 403)


def test_live_excepciones_row_carries_level_both_readings_and_case_data(
    client, db, people, monkeypatch
) -> None:
    """The UNCONFIRMED shape of a row: two readings and term_confirmed False.

    Pinned to 0 on purpose. The shipping default is 8 (one confirmed reading),
    so this row shape is no longer what the panel returns by default -- see
    test_by_default_the_panel_shows_one_confirmed_8_day_reading below.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "SEMAFORO_EXCEPCIONES_TERM_DAYS", 0)
    case = make_case(db, people)
    make_live(db, case)

    body = get(client)

    assert body["vencidos"] == []
    (row,) = body["por_vencer"]
    assert row["case_id"] == case.id
    assert row["rol"] == "C-1-2026"
    assert row["caratula"] == "Banco X/Deudor"
    assert row["tribunal"] == "1º Juzgado Civil"
    assert row["abogado_id"] == people["ana"].id
    assert row["abogado_nombre"] == "Ana Pérez"
    assert row["deadline_type"] == "excepciones_8d"
    assert row["label"] == "Plazo para oponer excepciones"
    assert row["level"] == "amarillo"
    assert row["term_confirmed"] is False
    assert [(r["term_days"], r["due_date"]) for r in row["readings"]] == [
        (4, "2026-11-06"), (8, "2026-11-11"),
    ]
    assert row["dias_habiles_remaining"] >= 0


def test_by_default_the_panel_shows_one_confirmed_8_day_reading(client, db, people) -> None:
    """What the lawyers actually see: ONE due date, no "Plazo no confirmado".

    The firm asked for the 4-day reading to be dropped and only the 8-day one
    kept, so SEMAFORO_EXCEPCIONES_TERM_DAYS defaults to 8. Nothing pinned here:
    this test exists to catch the default silently going back to 0, which is
    what put a "Plazo no confirmado: 4 días / 8 días" box on every excepciones
    plazo in the panel.
    """
    case = make_case(db, people)
    make_live(db, case)

    (row,) = get(client)["por_vencer"]

    assert row["term_confirmed"] is True
    assert [(r["term_days"], r["due_date"]) for r in row["readings"]] == [(8, "2026-11-11")]


def test_poder_is_confirmed_single_reading(client, db, people) -> None:
    case = make_case(db, people)
    # Poder ownership needs the movement heuristic; assess alone drives the level.
    make_live(db, case, "acreditar_poder_3d", triggered=TRIGGER)
    from unittest.mock import patch

    with patch("app.services.sync_service.poder_obligation_is_ours", return_value=object()):
        body = get(client)

    (row,) = body["por_vencer"]
    assert row["deadline_type"] == "acreditar_poder_3d"
    assert row["term_confirmed"] is True
    assert len(row["readings"]) == 1


def test_row_whose_assess_level_is_none_is_excluded(client, db, people) -> None:
    case = make_case(db, people)
    # Both readings are over, yet the stored due_date is still in the future.
    make_live(db, case, triggered=date(2026, 10, 1)).due_date = DAY3 + timedelta(days=5)
    db.commit()

    assert get(client)["por_vencer"] == []


def test_apelacion_is_not_on_the_panel(client, db, people) -> None:
    # The appeal is a right, not an obligation: the firm asked for it to stay out
    # of the semáforo, the home panel included.
    make_live(db, make_case(db, people), "apelacion_5d")

    assert get(client)["por_vencer"] == []


def test_counterparty_plazo_is_not_returned(client, db, people) -> None:
    case = make_case(db, people)
    db.add(CaseLitigante(
        case_id=case.id, participante="AB.DTE", rut=RUT,
        persona_type="NATURAL", nombre="Ana", natural_key="k1",
    ))
    db.commit()
    make_live(db, case)
    make_expired(db, case)

    body = get(client)

    assert body["por_vencer"] == [] and body["vencidos"] == []


def test_archived_case_or_inactive_deadline_excluded(client, db, people) -> None:
    make_live(db, make_case(db, people, rol="C-2-2026", status="archived"))
    done = make_live(db, make_case(db, people, rol="C-3-2026"))
    done.status = "cumplido"
    db.commit()

    assert get(client)["por_vencer"] == []


def test_vencidos_come_from_expired_unfiled_within_window(client, db, people, monkeypatch) -> None:
    case = make_case(db, people)
    recent = make_expired(db, case, due=date(2026, 10, 20))
    make_expired(db, case, due=date(2026, 9, 1))  # outside the 30 day window
    make_expired(db, case, due=date(2026, 10, 21), verdict="cumplido")  # we did act

    body = get(client)

    assert [r["deadline_id"] for r in body["vencidos"]] == [recent.id]
    row = body["vencidos"][0]
    assert row["level"] is None
    assert row["dias_habiles_remaining"] < 0
    assert row["rol"] == "C-1-2026"
    assert body["expired_window_days"] == 30


def test_window_is_the_existing_setting(client, db, people, monkeypatch) -> None:
    case = make_case(db, people)
    make_expired(db, case, due=date(2026, 9, 1))
    monkeypatch.setattr(settings, "SEMAFORO_EXPIRED_WINDOW_DAYS", 0)

    body = get(client)

    assert len(body["vencidos"]) == 1
    assert body["expired_window_days"] == 0


def test_expired_poder_is_not_returned(client, db, people) -> None:
    make_expired(db, make_case(db, people), "acreditar_poder_3d")

    assert get(client)["vencidos"] == []


def test_lawyer_sees_own_cases_admin_sees_firm(client, db, people) -> None:
    make_live(db, make_case(db, people, "ana", "C-1-2026"))
    make_live(db, make_case(db, people, "otro", "C-9-2026"))

    assert [r["rol"] for r in get(client, RUT)["por_vencer"]] == ["C-1-2026"]
    assert sorted(r["rol"] for r in get(client, ADMIN_RUT)["por_vencer"]) == [
        "C-1-2026", "C-9-2026",
    ]


def test_por_vencer_is_ordered_most_urgent_first(client, db, people) -> None:
    calm = make_live(db, make_case(db, people, rol="C-1-2026"), triggered=DAY3)
    hot = make_live(db, make_case(db, people, rol="C-2-2026"), triggered=date(2026, 10, 29))

    ids = [r["deadline_id"] for r in get(client)["por_vencer"]]

    assert ids.index(hot.id) < ids.index(calm.id)


def test_endpoint_is_read_only(client, db, people) -> None:
    case = make_case(db, people)
    make_live(db, case)
    make_expired(db, case)

    get(client)

    assert db.query(Alert).count() == 0


# -- terminated cases -------------------------------------------------------


def _set_state(db, case, state):
    case.procedural_state = state
    db.commit()


def test_terminada_case_is_excluded_from_both_groups(client, db, people) -> None:
    dead = make_case(db, people, rol="C-2143-2022")
    _set_state(db, dead, "terminada")
    make_live(db, dead)
    make_expired(db, dead)

    body = get(client)

    assert body["por_vencer"] == [] and body["vencidos"] == []


def test_only_terminada_is_filtered_other_states_and_null_stay(client, db, people) -> None:
    for i, state in enumerate([None, "en_tramite", "apelacion"], start=1):
        case = make_case(db, people, rol=f"C-{i}-2026")
        _set_state(db, case, state)
        make_live(db, case)
        make_expired(db, case)

    body = get(client)

    assert len(body["por_vencer"]) == 3
    assert len(body["vencidos"]) == 3
    assert all(r["case_id"] for r in body["por_vencer"])


# -- the same case stored under two lawyers ---------------------------------


def _twin(db, people, owner, court_name, rol="C-8085-2025"):
    court = Court(code=f"T-{uuid4().hex[:10]}", name=court_name, region="RM", type="civil")
    db.add(court)
    db.commit()
    case = Case(
        lawyer_id=people[owner].id, court_id=court.id, rol=rol,
        competencia="civil", plaintiff="Banco X", defendant="Deudor", status="active",
    )
    db.add(case)
    db.commit()
    return case


def test_same_case_under_two_lawyers_collapses_to_one_row_for_admin(client, db, people) -> None:
    first = make_live(db, _twin(db, people, "ana", "5º Juzgado Civil de Santiago"), "excepciones_8d")
    make_live(db, _twin(db, people, "otro", "5° JUZGADO CIVIL DE  SANTIAGO", rol="c-8085-2025"), "excepciones_8d")

    (row,) = get(client, ADMIN_RUT)["por_vencer"]

    assert row["deadline_id"] == first.id  # oldest record survives
    assert row["abogado_nombre"] == "Ana Pérez"
    assert row["abogados"] == ["Ana Pérez", "Otro Abogado"]


def test_expired_duplicates_collapse_too(client, db, people) -> None:
    make_expired(db, _twin(db, people, "ana", "5º Juzgado Civil de Santiago"))
    make_expired(db, _twin(db, people, "otro", "5º Juzgado Civil de Santiago"))

    assert len(get(client, ADMIN_RUT)["vencidos"]) == 1


def test_lawyer_own_view_is_unchanged_by_the_collapse(client, db, people) -> None:
    make_live(db, _twin(db, people, "ana", "5º Juzgado Civil de Santiago"), "excepciones_8d")
    make_live(db, _twin(db, people, "otro", "5º Juzgado Civil de Santiago"), "excepciones_8d")

    (row,) = get(client, RUT)["por_vencer"]

    assert row["abogado_nombre"] == "Ana Pérez"
    assert row["abogados"] == ["Ana Pérez"]


def test_different_court_type_or_due_date_are_not_collapsed(client, db, people) -> None:
    extra = [
        Lawyer(rut=f"4444444{n}-4", name=f"Extra {n}", email=f"e{n}@segal.cl", role="lawyer", is_active=True)
        for n in (1, 2)
    ]
    db.add_all(extra)
    db.commit()
    people = {**people, "e1": extra[0], "e2": extra[1]}
    court5 = "5º Juzgado Civil de Santiago"
    make_live(db, _twin(db, people, "ana", court5, "C-1-2026"), "excepciones_8d")
    make_live(db, _twin(db, people, "otro", "6º Juzgado Civil de Santiago", "C-1-2026"), "excepciones_8d")
    make_live(
        db, _twin(db, people, "e1", court5, "C-1-2026"), "excepciones_8d",
        triggered=date(2026, 11, 3),
    )
    make_live(db, _twin(db, people, "e2", court5, "C-1-2026"), "acreditar_poder_3d")
    from unittest.mock import patch

    with patch("app.services.sync_service.poder_obligation_is_ours", return_value=object()):
        assert len(get(client, ADMIN_RUT)["por_vencer"]) == 4



class TestVencidosCarryTheirVerdict:
    """A plazo filed LATE is not a plazo never filed, and the panel must tell
    them apart.

    Measured on the real database: of the 9 rows the panel showed under
    "vencidos sin presentar", 6 carried verdict ``fuera_de_plazo`` -- the
    escrito IS in our own ``case_escritos`` ("Opone excepciones", Demandado,
    09-oct). Telling a lawyer he never filed something he filed is the fastest
    way to make him stop trusting the panel.
    """

    def test_a_late_filing_is_distinguishable_from_one_never_filed(
        self, client, db, people
    ) -> None:
        tarde = make_case(db, people, rol="C-900-2026")
        make_expired(db, tarde, verdict="fuera_de_plazo")
        nunca = make_case(db, people, rol="C-901-2026")
        make_expired(db, nunca, verdict="no_cumplido")
        db.commit()

        filas = {f["rol"]: f for f in get(client)["vencidos"]}
        assert filas["C-900-2026"]["verdict"] == "fuera_de_plazo"
        assert filas["C-901-2026"]["verdict"] == "no_cumplido"


def _audit(db, row, people, status, by_auditor=True):
    from datetime import datetime

    row.status = status
    if by_auditor:
        row.marked_by = people["ana"].id
        row.marked_at = datetime(2026, 10, 27, 12, 0)
    db.commit()


def test_vencido_confirmed_cumplido_by_an_auditor_is_not_listed(client, db, people) -> None:
    """The verdict is the engine's evidence; an auditor's cumplido overrides it.

    Without this, a plazo an auditor confirmed as met keeps showing in the
    "vencidos" list (and in the expired-notice alerts) because its verdict still
    says fuera_de_plazo."""
    case = make_case(db, people)
    row = make_expired(db, case, verdict="fuera_de_plazo")
    _audit(db, row, people, "cumplido")

    assert get(client)["vencidos"] == []


def test_vencido_cumplido_without_an_auditor_is_still_listed(client, db, people) -> None:
    """Only an auditor-signed cumplido counts; a bare status change does not."""
    case = make_case(db, people)
    row = make_expired(db, case, verdict="fuera_de_plazo")
    _audit(db, row, people, "cumplido", by_auditor=False)

    assert [r["deadline_id"] for r in get(client)["vencidos"]] == [row.id]


def test_vencido_marked_no_cumplido_by_an_auditor_is_still_listed(client, db, people) -> None:
    case = make_case(db, people)
    row = make_expired(db, case, verdict="no_cumplido")
    _audit(db, row, people, "no_cumplido")

    assert [r["deadline_id"] for r in get(client)["vencidos"]] == [row.id]
