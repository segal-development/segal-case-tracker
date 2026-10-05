"""Engine-marked ``cumplido`` rows (no auditor) must not flood the audited list."""

from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.lawyer import Lawyer

RUT = "55555555-5"


@pytest.fixture
def headers(db):
    db.add(Lawyer(rut=RUT, name="Case Lawyer", role="lawyer"))
    db.commit()
    tok = create_access_token({"sub": RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def test_audited_list_only_contains_rows_marked_by_a_human(client, db, headers) -> None:
    lawyer = db.query(Lawyer).filter(Lawyer.rut == RUT).one()
    case = Case(rol="C-1-2026", lawyer_id=lawyer.id, court_id=1)
    db.add(case)
    db.commit()
    base = dict(case_id=case.id, due_date=date.today(), triggered_at=date.today())
    db.add_all([
        CaseDeadline(deadline_type="apelacion_5d", status="cumplido",
                     marked_by=lawyer.id, marked_at=datetime(2026, 6, 1), **base),
        # Set by the engine itself (compliance detected from a movement): no marked_at.
        CaseDeadline(deadline_type="acreditar_poder_3d", status="cumplido", **base),
    ])
    other = dict(base, triggered_at=date.today() - timedelta(days=1))
    # Same type, but an auditor marked it: that IS an audit and must show.
    db.add(CaseDeadline(deadline_type="acreditar_poder_3d", status="no_cumplido",
                        marked_by=lawyer.id, marked_at=datetime(2026, 6, 2), **other))
    db.commit()

    resp = client.get("/api/v1/cases/deadlines/audited", headers=headers)

    assert resp.status_code == 200
    got = sorted((r["deadline_type"], r["status"]) for r in resp.json())
    assert got == [("acreditar_poder_3d", "no_cumplido"), ("apelacion_5d", "cumplido")]
