"""Priority document requests in the station's case rotation and doc cap."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.services.sync_service import (
    _cap_historical_documents,
    _select_cases_for_detail_rotation,
)
from tests.services.test_detail_rotation import _make_api_case


def _setup(db, rut="00000077-7"):
    lawyer = Lawyer(rut=rut, name="Prio", is_active=True)
    db.add(lawyer)
    db.flush()
    court = db.query(Court).first()
    if not court:
        court = Court(code="PR-COURT", name="Prio Court", region="RM", type="civil")
        db.add(court)
        db.flush()
    return lawyer, court


def _case(db, lawyer, court, rol, filed_at=None):
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", last_detail_checked_at=None, filed_at=filed_at,
    )
    db.add(c)
    db.flush()
    return c


class TestRotationPriority:
    def test_case_with_priority_doc_goes_first_oldest_request_first(self, db):
        lawyer, court = _setup(db)
        now = datetime.utcnow()
        # filed_at DESC would normally order: C-A, C-B, C-C, C-D
        a = _case(db, lawyer, court, "C-1-2026", now)
        _case(db, lawyer, court, "C-2-2026", now - timedelta(days=1))
        c = _case(db, lawyer, court, "C-3-2026", now - timedelta(days=2))
        d = _case(db, lawyer, court, "C-4-2026", now - timedelta(days=3))
        db.add(Document(case_id=c.id, doc_type="resolution", status="pending",
                        prioridad_solicitada_at=now - timedelta(minutes=5)))
        db.add(Document(case_id=d.id, doc_type="resolution", status="failed",
                        prioridad_solicitada_at=now - timedelta(minutes=30)))
        # stored docs with a request must not count
        db.add(Document(case_id=a.id, doc_type="resolution", status="stored",
                        prioridad_solicitada_at=now - timedelta(hours=9)))
        db.commit()

        api = [_make_api_case(r) for r in
               ("C-1-2026", "C-2-2026", "C-3-2026", "C-4-2026")]
        result = _select_cases_for_detail_rotation(
            db, lawyer.id, "civil", api, batch_size=10
        )
        assert [ac.rol for ac in result] == [
            "C-4-2026", "C-3-2026", "C-1-2026", "C-2-2026"
        ]

    def test_no_priority_keeps_existing_order(self, db):
        lawyer, court = _setup(db, "00000078-8")
        now = datetime.utcnow()
        _case(db, lawyer, court, "C-1-2026", now)
        _case(db, lawyer, court, "C-2-2026", now - timedelta(days=1))
        db.commit()
        api = [_make_api_case("C-2-2026"), _make_api_case("C-1-2026")]
        result = _select_cases_for_detail_rotation(
            db, lawyer.id, "civil", api, batch_size=10
        )
        assert [ac.rol for ac in result] == ["C-1-2026", "C-2-2026"]


def _doc(i, *, requested=None, days_old=400, movement_id=None):
    return SimpleNamespace(
        id=i,
        document_date=datetime.utcnow() - timedelta(days=days_old),
        movement_id=movement_id,
        prioridad_solicitada_at=requested,
    )


class TestCapPriority:
    def test_priority_historical_kept_when_historical_disabled(self):
        prio = _doc(1, requested=datetime.utcnow())
        old = _doc(2)
        recent = _doc(3, days_old=1)
        kept = _cap_historical_documents(
            [old, prio, recent], recent_days=30, cap=0, rol="C-1-2026",
            historical_enabled=False,
        )
        assert {d.id for d in kept} == {1, 3}

    def test_priority_survives_cap_and_is_chosen_first(self):
        t = datetime.utcnow()
        prio = _doc(1, requested=t, days_old=900)
        newer = [_doc(i, days_old=100 + i) for i in range(2, 6)]
        kept = _cap_historical_documents(
            newer + [prio], recent_days=30, cap=2, rol="C-1-2026",
        )
        ids = [d.id for d in kept]
        assert 1 in ids
        assert len(ids) == 2  # counts against the cap
        assert ids[-1] == 1 or ids[0] == 1

    def test_priority_beyond_cap_all_kept_oldest_request_first(self):
        t = datetime.utcnow()
        p1 = _doc(1, requested=t - timedelta(minutes=1))
        p2 = _doc(2, requested=t - timedelta(minutes=9))
        kept = _cap_historical_documents(
            [p1, p2, _doc(3)], recent_days=30, cap=1, rol="C-1-2026",
        )
        assert [d.id for d in kept][:2] == [2, 1]
        assert 3 not in {d.id for d in kept}
