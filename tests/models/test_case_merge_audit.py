"""Tests for the CaseMergeAudit model (ADR-008 Slice 2 reversibility ledger).

Records loser->winner case_id mappings written by
``app.services.case_merge.run_case_merge`` BEFORE the loser Case row is
deleted, so the merge is auditable/reversible after the fact.
"""

from datetime import datetime

import pytest

from app.models.case import Case
from app.models.case_merge_audit import CaseMergeAudit
from app.models.court import Court
from app.models.lawyer import Lawyer


@pytest.fixture
def seeded_cases(db):
    firm = Lawyer(rut="16021492-9", name="Firm Lawyer")
    sandy = Lawyer(rut="11111111-1", name="Sandy")
    db.add_all([firm, sandy])
    db.flush()
    court = Court(code="T1-CMA", name="Juzgado CaseMergeAudit Test", region="RM", type="civil")
    db.add(court)
    db.flush()
    winner = Case(lawyer_id=firm.id, court_id=court.id, rol="C-1-2026", competencia="civil")
    loser = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-1-2026-dup", competencia="civil")
    db.add_all([winner, loser])
    db.commit()
    return {"firm": firm, "sandy": sandy, "winner": winner, "loser": loser}


class TestCaseMergeAuditModel:
    def test_create_audit_row(self, db, seeded_cases):
        loser = seeded_cases["loser"]
        winner = seeded_cases["winner"]
        sandy = seeded_cases["sandy"]

        row = CaseMergeAudit(
            loser_case_id=loser.id,
            winner_case_id=winner.id,
            loser_lawyer_id=sandy.id,
            rol="C-1234-2024",
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        assert row.id is not None
        assert row.loser_case_id == loser.id
        assert row.winner_case_id == winner.id
        assert row.loser_lawyer_id == sandy.id
        assert row.rol == "C-1234-2024"

    def test_survives_loser_case_deletion(self, db, seeded_cases):
        """loser_case_id is NOT a FK — the row must persist after the loser
        Case is deleted (that is the whole point of the audit trail)."""
        loser = seeded_cases["loser"]
        winner = seeded_cases["winner"]
        sandy = seeded_cases["sandy"]
        loser_id = loser.id

        row = CaseMergeAudit(
            loser_case_id=loser_id,
            winner_case_id=winner.id,
            loser_lawyer_id=sandy.id,
            rol="C-1-2026",
        )
        db.add(row)
        db.commit()

        db.delete(loser)
        db.commit()

        db.refresh(row)
        assert row.loser_case_id == loser_id
        assert db.query(Case).filter(Case.id == loser_id).first() is None
