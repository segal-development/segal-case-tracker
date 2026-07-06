"""Tests for the CaseLawyerSource model (ADR-002 M:N sync-source association).

Records "lawyer L has seen ROL R in their live PJUD Mis Causas list" —
preserves the per-syncing-lawyer signal that ``Case.lawyer_id`` used to carry
before ingest started upserting one Case per ROL under the firm lawyer_id
(Approach C). Consumed by ``get_pending_detail`` (task 1b-5).
"""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.lawyer import Lawyer


@pytest.fixture
def seeded_case(db):
    lawyer = Lawyer(rut="16021492-9", name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    court = Court(code="T1-CLS", name="Juzgado CaseLawyerSource Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-1-2026", competencia="civil")
    db.add(case)
    db.commit()
    return case, lawyer


class TestCaseLawyerSourceModel:
    def test_create_case_lawyer_source_row(self, db, seeded_case):
        case, lawyer = seeded_case
        now = datetime.utcnow()
        row = CaseLawyerSource(
            case_id=case.id,
            lawyer_id=lawyer.id,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        assert row.id is not None
        assert row.case_id == case.id
        assert row.lawyer_id == lawyer.id
        assert row.first_seen_at is not None
        assert row.last_seen_at is not None

    def test_unique_constraint_case_id_lawyer_id(self, db, seeded_case):
        case, lawyer = seeded_case
        db.add(CaseLawyerSource(case_id=case.id, lawyer_id=lawyer.id))
        db.commit()

        db.add(CaseLawyerSource(case_id=case.id, lawyer_id=lawyer.id))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_index_on_lawyer_id(self, db, seeded_case):
        """The lawyer_id index exists (query executes; smoke-checks the mapping)."""
        case, lawyer = seeded_case
        db.add(CaseLawyerSource(case_id=case.id, lawyer_id=lawyer.id))
        db.commit()

        rows = db.query(CaseLawyerSource).filter(CaseLawyerSource.lawyer_id == lawyer.id).all()
        assert len(rows) == 1
        indexed_cols = [c.name for c in CaseLawyerSource.__table__.columns if c.index]
        assert "lawyer_id" in indexed_cols
