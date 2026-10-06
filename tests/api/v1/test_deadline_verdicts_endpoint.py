"""GET /cases/deadlines/verdicts — engine-computed verdicts for the Plazos screen.

Kept separate from ``/cases/deadlines/audited`` on purpose: that one returns
HUMAN audits only, and the engine never gives a verdict to a human-marked row
(see test_audited_excludes_engine_marked.py and
TestAuditorMarksWin::test_audited_row_is_not_given_a_verdict).  Mixing the two
would break both invariants.
"""

from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer

RUT = "66666666-6"
OTHER_RUT = "77777777-7"


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def lawyer(db):
    lw = Lawyer(rut=RUT, name="Dueña de la causa", role="lawyer")
    db.add(lw)
    db.commit()
    return lw


@pytest.fixture
def case(db, lawyer):
    c = Case(rol="C-100-2026", lawyer_id=lawyer.id, court_id=1)
    db.add(c)
    db.commit()
    return c


def _dl(case, **kw) -> CaseDeadline:
    base = dict(
        case_id=case.id,
        deadline_type="excepciones_8d",
        due_date=date(2026, 5, 10),
        triggered_at=date(2026, 5, 1),
        status="superseded",
    )
    base.update(kw)
    return CaseDeadline(**base)


def test_returns_rows_that_have_a_verdict(client, db, case) -> None:
    db.add_all([
        _dl(case, verdict="cumplido", verdict_acted_on=date(2026, 5, 8)),
        _dl(case, verdict="fuera_de_plazo", verdict_acted_on=date(2026, 6, 20),
            deadline_type="apelacion_5d"),
    ])
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(RUT))

    assert resp.status_code == 200
    got = sorted((r["deadline_type"], r["verdict"]) for r in resp.json())
    assert got == [("apelacion_5d", "fuera_de_plazo"), ("excepciones_8d", "cumplido")]


def test_excludes_rows_without_a_verdict(client, db, case) -> None:
    """An active deadline with no verdict yet is not a verdict row."""
    db.add_all([
        _dl(case, verdict="cumplido", verdict_acted_on=date(2026, 5, 8)),
        _dl(case, status="active", verdict=None, deadline_type="apelacion_5d"),
    ])
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(RUT))

    assert resp.status_code == 200
    assert [r["deadline_type"] for r in resp.json()] == ["excepciones_8d"]


def test_payload_carries_what_the_screen_needs_to_tier_the_delay(client, db, case) -> None:
    """``verdict_acted_on`` plus ``due_date`` is what lets the UI separate a
    delay that exceeds the measurement error from one inside PJUD's publication
    noise.  Without both, the screen can only accuse."""
    db.add(_dl(case, verdict="fuera_de_plazo", verdict_acted_on=date(2026, 6, 20)))
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(RUT))

    row = resp.json()[0]
    assert row["verdict"] == "fuera_de_plazo"
    assert row["verdict_acted_on"] == "2026-06-20"
    assert row["due_date"] == "2026-05-10"
    assert row["rol"] == "C-100-2026"
    assert row["label"] == "Plazo para oponer excepciones"
    assert row["abogado_nombre"] == "Dueña de la causa"


def test_scoped_to_the_owning_lawyer(client, db, case) -> None:
    db.add(Lawyer(rut=OTHER_RUT, name="Ajeno", role="lawyer"))
    db.add(_dl(case, verdict="cumplido", verdict_acted_on=date(2026, 5, 8)))
    db.commit()

    mine = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(RUT))
    theirs = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(OTHER_RUT))

    assert len(mine.json()) == 1
    assert theirs.json() == []


def test_human_audited_rows_do_not_leak_into_the_verdict_list(client, db, case, lawyer) -> None:
    """The human mark wins and suppresses the engine verdict; such a row belongs
    to /audited, not here."""
    db.add(_dl(case, status="no_cumplido", marked_by=lawyer.id,
               marked_at=datetime(2026, 6, 1), verdict=None))
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers(RUT))

    assert resp.json() == []


def test_admin_sees_the_whole_firm(client, db, case) -> None:
    """Dirección Jurídica is role ``admin`` and owns no causas of her own.

    If this endpoint were scoped like a regular lawyer's, she would open the
    screen and see nothing -- the same failure mode as the "sin datos" sidebar
    bug.  ``resolve_case_scope`` grants ALL_CASES to auditor AND admin
    (deps.py); this test is what keeps that true.
    """
    db.add(Lawyer(rut="88888888-8", name="Dirección Jurídica", role="admin"))
    db.add(_dl(case, verdict="registro_tardio", verdict_acted_on=date(2026, 5, 12)))
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/verdicts", headers=_headers("88888888-8"))

    assert resp.status_code == 200
    assert [r["verdict"] for r in resp.json()] == ["registro_tardio"]
