"""A case that an EARLIER merge recorded as a winner can lose a LATER merge.

``case_merge_audit.winner_case_id`` is a foreign key to ``cases.id``. The merge of
migration 024 left 229 audit rows, and 167 of the cases that are still duplicated today
appear in them as winners. When one of those cases loses a new merge, deleting it
violated the foreign key on PostgreSQL (``case_merge_audit_winner_case_id_fkey``) and the
whole batch was rolled back. SQLite in the test suite does not enforce foreign keys, so
these tests assert the references directly.

The audit trail is kept, not deleted: the old row now says the loser went into the
NEW winner, which is where its data lives.
"""

import pytest

from app.models.case import Case
from app.models.case_merge_audit import CaseMergeAudit
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.case_merge import (
    CaseMergeVerificationError,
    _verify_invariants,
    run_case_merge,
)

FIRM_RUT = "16021492-9"


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def world(db, firm):
    other = Lawyer(rut="11111111-1", name="Other")
    court = Court(code="T-AUD", name="Juzgado Audit", region="RM", type="civil")
    db.add_all([other, court])
    db.flush()
    return firm, other, court


def _case(db, lawyer, court, rol, procedure="Principal"):
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, procedure=procedure, competencia="civil")
    db.add(case)
    db.flush()
    return case


def _give_history(db, case, n):
    from datetime import datetime

    for i in range(n):
        db.add(Movement(case_id=case.id, folio=str(i), description=f"mov {i}", movement_date=datetime(2026, 1, 1 + i)))
    db.flush()


def _old_audit(db, winner_case_id, lawyer_id, rol="C-1-2026"):
    """An audit row left by a previous merge: some long-gone loser went into ``winner_case_id``."""
    row = CaseMergeAudit(loser_case_id=999_001, winner_case_id=winner_case_id, loser_lawyer_id=lawyer_id, rol=rol)
    db.add(row)
    db.flush()
    return row


def _existing_ids(db):
    return {cid for (cid,) in db.query(Case.id)}


class TestOldWinnerLosesALaterMerge:
    def test_old_audit_row_follows_the_new_winner(self, db, world):
        firm, other, court = world
        old_winner = _case(db, firm, court, "C-1-2026")          # empty, id lower
        new_winner = _case(db, other, court, "C-1-2026")         # richer: wins
        _give_history(db, new_winner, 3)
        old_row = _old_audit(db, old_winner.id, other.id)
        old_id, new_id = old_winner.id, new_winner.id

        run_case_merge(db)
        db.flush()

        assert old_id not in _existing_ids(db)                  # the loser is gone
        db.refresh(old_row)
        assert old_row.winner_case_id == new_id                  # the history follows it

    def test_no_audit_row_references_a_deleted_case(self, db, world):
        firm, other, court = world
        a = _case(db, firm, court, "C-1-2026")
        b = _case(db, other, court, "C-1-2026")
        _give_history(db, b, 2)
        _old_audit(db, a.id, other.id)

        run_case_merge(db)
        db.flush()

        alive = _existing_ids(db)
        dangling = [r.winner_case_id for r in db.query(CaseMergeAudit) if r.winner_case_id not in alive]
        assert dangling == []

    def test_the_new_merge_is_audited_too(self, db, world):
        firm, other, court = world
        a = _case(db, firm, court, "C-1-2026")
        b = _case(db, other, court, "C-1-2026")
        _give_history(db, b, 2)
        _old_audit(db, a.id, other.id)
        a_id, b_id = a.id, b.id

        run_case_merge(db)
        db.flush()

        new = db.query(CaseMergeAudit).filter(CaseMergeAudit.loser_case_id == a_id).all()
        assert [r.winner_case_id for r in new] == [b_id]

    def test_works_in_a_batch_and_with_several_references(self, db, world):
        firm, other, court = world
        a = _case(db, firm, court, "C-1-2026")
        b = _case(db, other, court, "C-1-2026")
        _give_history(db, b, 2)
        rows = [_old_audit(db, a.id, other.id) for _ in range(3)]
        b_id = b.id

        run_case_merge(db, group_limit=1)
        db.flush()

        for r in rows:
            db.refresh(r)
        assert {r.winner_case_id for r in rows} == {b_id}

    def test_a_case_that_wins_keeps_its_old_audit_rows(self, db, world):
        firm, other, court = world
        a = _case(db, firm, court, "C-1-2026")
        b = _case(db, other, court, "C-1-2026")
        _give_history(db, a, 3)                                  # a is the richer one and wins
        row = _old_audit(db, a.id, other.id)
        a_id = a.id

        run_case_merge(db)
        db.flush()

        db.refresh(row)
        assert row.winner_case_id == a_id


class TestVerifyCatchesAReferenceThatWasNotRepointed:
    def test_verification_refuses_when_an_audit_row_still_points_at_a_loser(self, db, world):
        firm, other, court = world
        loser = _case(db, firm, court, "C-1-2026")
        _case(db, other, court, "C-1-2026")
        _old_audit(db, loser.id, other.id)

        with pytest.raises(CaseMergeVerificationError, match="case_merge_audit"):
            _verify_invariants(db, [loser.id])
