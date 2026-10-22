"""Tests for resumable, batched case merges (group_limit / only_group_keys).

Spec odd/tasks/duplicados-por-abogado.md, T5b1: the real cleanup runs in
batches, each its own transaction. A batch must verify and re-own ONLY its own
groups, and a re-run must pick the NEXT remaining groups.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.case_merge_audit import CaseMergeAudit
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services import case_merge
from app.services.case_merge import (
    CaseMergeVerificationError,
    count_duplicate_identity_groups,
    run_case_merge,
)

FIRM_RUT = "16021492-9"


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    return lawyer


def _lawyer(db, rut, name):
    lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
    if lawyer is None:
        lawyer = Lawyer(rut=rut, name=name)
        db.add(lawyer)
        db.flush()
    return lawyer


def _court(db, code):
    court = db.query(Court).filter(Court.code == code).first()
    if court is None:
        court = Court(code=code, name=f"Juzgado {code}", region="RM", type="civil")
        db.add(court)
        db.flush()
    return court


def _case(db, lawyer, court, rol, procedure=None):
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        procedure=procedure,
        competencia="civil",
    )
    db.add(case)
    db.flush()
    return case


def _movement(case_id, folio, description):
    return Movement(
        case_id=case_id,
        folio=folio,
        description=description,
        movement_date=datetime.utcnow() - timedelta(days=1),
    )


def _rol(i):
    return f"C-{i}-2024"


def _build_groups(db, rols, court_code="B-A", individually_owned_winner=True):
    """One duplicate group per ROL. The winner (richest) is owned by Carla,
    never by the firm, so a re-own is observable. The loser (Sandy) carries
    one movement of its own that must be unioned onto the winner."""
    carla = _lawyer(db, "22222222-2", "Carla")
    sandy = _lawyer(db, "11111111-1", "Sandy")
    court = _court(db, court_code)
    groups = {}
    for rol in rols:
        winner = _case(db, carla, court, rol)
        loser = _case(db, sandy, court, rol)
        db.add_all(
            [
                _movement(winner.id, "1", f"{rol} base"),
                _movement(winner.id, "2", f"{rol} shared"),
                _movement(loser.id, "2", f"{rol} shared"),
                _movement(loser.id, "3", f"{rol} only-loser"),
            ]
        )
        groups[rol] = (winner.id, loser.id)
    db.flush()
    return court, groups


def _identity(court, rol, procedure=""):
    return (rol, court.id, procedure)


def _copies(db, court, rol):
    return db.query(Case).filter(Case.court_id == court.id, Case.rol == rol).all()


def _signature(db):
    """Final state normalized away from surrogate ids."""
    ruts = {lw.id: lw.rut for lw in db.query(Lawyer).all()}
    cases = db.query(Case).order_by(Case.rol).all()
    rol_by_case = {c.id: (c.rol, c.court_id, c.procedure or "") for c in cases}
    return {
        "cases": sorted((c.rol, c.court_id, c.procedure or "", ruts[c.lawyer_id]) for c in cases),
        "movements": sorted(
            (rol_by_case[m.case_id], m.folio, m.description) for m in db.query(Movement).all()
        ),
        "sources": sorted(
            (rol_by_case[s.case_id], ruts[s.lawyer_id]) for s in db.query(CaseLawyerSource).all()
        ),
        "audit": sorted(
            (a.rol, ruts[a.loser_lawyer_id]) for a in db.query(CaseMergeAudit).all()
        ),
    }


class TestGroupLimit:
    def test_limit_merges_exactly_n_groups_and_leaves_the_rest_untouched(self, db, firm):
        court, groups = _build_groups(db, [_rol(i) for i in range(5)])

        result = run_case_merge(db, group_limit=2)

        assert result.merged_rol_count == 2
        assert result.losers_deleted == 2
        assert result.audit_rows_created == 2
        assert result.remaining_groups == 3
        assert len(result.group_keys) == 2
        merged = {key[0] for key in result.group_keys}
        for rol, (winner_id, loser_id) in groups.items():
            copies = _copies(db, court, rol)
            if rol in merged:
                assert [c.id for c in copies] == [winner_id]
            else:
                assert sorted(c.id for c in copies) == sorted([winner_id, loser_id])
                assert db.query(Movement).filter(Movement.case_id == winner_id).count() == 2
                assert db.query(Movement).filter(Movement.case_id == loser_id).count() == 2
                assert db.query(CaseMergeAudit).filter(CaseMergeAudit.rol == rol).count() == 0

    def test_groups_outside_the_batch_are_not_reowned(self, db, firm):
        court, groups = _build_groups(db, [_rol(i) for i in range(5)])

        result = run_case_merge(db, group_limit=2)

        merged = {key[0] for key in result.group_keys}
        assert result.reowned_case_count == 2
        for rol, (winner_id, loser_id) in groups.items():
            owners = {c.lawyer_id for c in _copies(db, court, rol)}
            if rol in merged:
                assert owners == {firm.id}
            else:
                assert firm.id not in owners

    def test_successive_calls_pick_the_next_groups_until_none_remain(self, db, firm):
        court, _ = _build_groups(db, [_rol(i) for i in range(5)])

        first = run_case_merge(db, group_limit=2)
        second = run_case_merge(db, group_limit=2)
        third = run_case_merge(db, group_limit=2)
        fourth = run_case_merge(db, group_limit=2)

        keys = [first.group_keys, second.group_keys, third.group_keys]
        assert [len(k) for k in keys] == [2, 2, 1]
        flat = [k for batch in keys for k in batch]
        assert len(set(flat)) == 5
        assert (first.remaining_groups, second.remaining_groups, third.remaining_groups) == (3, 1, 0)
        assert fourth.merged_rol_count == 0
        assert fourth.group_keys == []
        assert fourth.remaining_groups == 0
        assert db.query(Case).count() == 5

    def test_groups_are_taken_in_sorted_identity_order(self, db, firm):
        # Created out of order on purpose; the order is (court_id, rol, procedure).
        court, _ = _build_groups(db, [_rol(3), _rol(1), _rol(2)])

        first = run_case_merge(db, group_limit=1)
        second = run_case_merge(db, group_limit=1)

        assert first.group_keys == [_identity(court, _rol(1))]
        assert second.group_keys == [_identity(court, _rol(2))]

    def test_no_argument_call_keeps_the_full_table_behaviour(self, db, firm):
        court, groups = _build_groups(db, [_rol(i) for i in range(3)])
        result = run_case_merge(db)

        assert result.merged_rol_count == 3
        assert result.remaining_groups == 0
        assert len(result.group_keys) == 3
        assert {c.lawyer_id for c in db.query(Case).all()} == {firm.id}

    def test_batches_then_full_run_equal_a_single_full_run(self, db, firm):
        rols = [_rol(i) for i in range(6)]
        _build_groups(db, rols)
        # A non-duplicate, individually owned case is re-owned by the final
        # no-argument run exactly like in the control run.
        carla = _lawyer(db, "22222222-2", "Carla")
        _case(db, carla, _court(db, "B-SOLO"), "C-solo-2024")

        control_engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(bind=control_engine)
        control = sessionmaker(bind=control_engine, autoflush=False)()
        try:
            _lawyer(control, FIRM_RUT, "Firm Lawyer")
            _build_groups(control, rols)
            _case(control, _lawyer(control, "22222222-2", "Carla"), _court(control, "B-SOLO"), "C-solo-2024")
            run_case_merge(control)
            expected = _signature(control)
        finally:
            control.close()
            control_engine.dispose()

        run_case_merge(db, group_limit=2)
        run_case_merge(db, group_limit=3)
        run_case_merge(db)

        assert _signature(db) == expected


class TestOnlyGroupKeys:
    def test_merges_just_the_given_groups_and_ignores_unknown_keys(self, db, firm):
        court, groups = _build_groups(db, [_rol(i) for i in range(4)])
        wanted = [_identity(court, _rol(1)), _identity(court, _rol(3))]
        unknown = [_identity(court, "C-999-2024"), (_rol(1), court.id + 999, "")]

        result = run_case_merge(db, only_group_keys=wanted + unknown)

        assert sorted(result.group_keys) == sorted(wanted)
        assert result.merged_rol_count == 2
        assert result.remaining_groups == 2
        for rol, (winner_id, loser_id) in groups.items():
            expected = 1 if _identity(court, rol) in wanted else 2
            assert len(_copies(db, court, rol)) == expected

    def test_resuming_with_already_merged_keys_is_a_noop(self, db, firm):
        court, _ = _build_groups(db, [_rol(i) for i in range(2)])
        keys = [_identity(court, _rol(0))]
        run_case_merge(db, only_group_keys=keys)

        again = run_case_merge(db, only_group_keys=keys)

        assert again.merged_rol_count == 0
        assert again.losers_deleted == 0
        assert again.group_keys == []
        assert again.remaining_groups == 1

    def test_none_and_empty_procedure_keys_match_the_same_group(self, db, firm):
        carla = _lawyer(db, "22222222-2", "Carla")
        sandy = _lawyer(db, "11111111-1", "Sandy")
        court = _court(db, "B-A")
        a = _case(db, carla, court, _rol(1), procedure=None)
        _case(db, sandy, court, _rol(1), procedure="")

        result = run_case_merge(db, only_group_keys=[_identity(court, _rol(1), "")])

        assert result.merged_rol_count == 1
        assert [c.id for c in db.query(Case).all()] == [a.id]

    def test_limit_applies_after_the_key_filter(self, db, firm):
        court, _ = _build_groups(db, [_rol(i) for i in range(5)])
        keys = [_identity(court, _rol(i)) for i in (1, 2, 3)]

        result = run_case_merge(db, only_group_keys=keys, group_limit=2)

        assert result.group_keys == [_identity(court, _rol(1)), _identity(court, _rol(2))]
        assert result.remaining_groups == 3

    def test_same_rol_in_another_court_is_never_touched(self, db, firm):
        court, groups = _build_groups(db, [_rol(1)], court_code="B-A")
        other_court, other_groups = _build_groups(db, [_rol(1)], court_code="B-B")

        by_limit = run_case_merge(db, group_limit=1)

        assert by_limit.group_keys == [_identity(court, _rol(1))]
        assert len(_copies(db, other_court, _rol(1))) == 2
        assert db.query(Movement).filter(Movement.case_id.in_(other_groups[_rol(1)])).count() == 4

        by_keys = run_case_merge(db, only_group_keys=[_identity(court, _rol(1))])

        assert by_keys.merged_rol_count == 0
        assert len(_copies(db, other_court, _rol(1))) == 2


class TestScopedVerification:
    def test_partial_batch_is_not_blocked_by_groups_outside_the_batch(self, db, firm):
        court, _ = _build_groups(db, [_rol(i) for i in range(3)])

        # The full-table check would fail here: two groups still hold copies.
        with pytest.raises(CaseMergeVerificationError):
            case_merge._verify_invariants(db, [])

        result = run_case_merge(db, group_limit=1)

        assert result.merged_rol_count == 1

    def test_stray_child_row_inside_the_batch_aborts_with_nothing_deleted(
        self, db, firm, monkeypatch
    ):
        court, groups = _build_groups(db, [_rol(i) for i in range(3)])
        monkeypatch.setattr(case_merge, "_merge_children", lambda db, loser_id, winner_id: None)

        with pytest.raises(CaseMergeVerificationError, match="movements"):
            run_case_merge(db, group_limit=1)

        assert db.query(Case).count() == 6

    def test_duplicates_left_inside_the_batch_fail_the_scoped_check(self, db, firm):
        court, groups = _build_groups(db, [_rol(0), _rol(1)])
        inside = [_identity(court, _rol(0))]

        # Nothing was merged, so the in-batch group still has two survivors.
        with pytest.raises(CaseMergeVerificationError, match=_rol(0)):
            case_merge._verify_invariants(db, [], identities=inside)

    def test_scoped_check_ignores_duplicates_outside_the_identities(self, db, firm):
        court, groups = _build_groups(db, [_rol(0), _rol(1)])

        case_merge._verify_invariants(db, [], identities=[])
        case_merge._verify_invariants(db, [], identities=[_identity(court, "C-999-2024")])


class TestCountDuplicateIdentityGroups:
    def test_matches_the_groups_found(self, db, firm):
        court, _ = _build_groups(db, [_rol(i) for i in range(4)])
        carla = _lawyer(db, "22222222-2", "Carla")
        sandy = _lawyer(db, "11111111-1", "Sandy")
        # Same ROL in another court and another cuaderno: separate identities.
        _case(db, carla, _court(db, "B-B"), _rol(0))
        _case(db, sandy, court, _rol(0), procedure="Medida prejudicial")

        assert count_duplicate_identity_groups(db) == len(case_merge._find_duplicate_identity_groups(db)) == 4

        run_case_merge(db, group_limit=3)

        assert count_duplicate_identity_groups(db) == len(case_merge._find_duplicate_identity_groups(db)) == 1

    def test_is_a_single_statement(self, db, firm):
        _build_groups(db, [_rol(i) for i in range(5)])
        statements = _count_statements(db, lambda: count_duplicate_identity_groups(db))

        assert statements == 1


def _count_statements(db, fn):
    engine = db.get_bind()
    seen = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _before)
    return len(seen)


class TestStatementCountDoesNotScaleWithOutsideGroups:
    def test_batch_cost_is_independent_of_groups_outside_it(self, db, firm):
        # Courts sort by id, so the "B-A" group is always the batch and the
        # "B-Z" groups are always outside it.
        _build_groups(db, ["C-in-1"], court_code="B-A")
        _build_groups(db, [_rol(i) for i in range(3)], court_code="B-Z")
        few = _count_statements(db, lambda: run_case_merge(db, group_limit=1))

        _build_groups(db, ["C-in-2"], court_code="B-A")
        _build_groups(db, [_rol(i) for i in range(3, 30)], court_code="B-Z")
        many = _count_statements(db, lambda: run_case_merge(db, group_limit=1))

        assert few == many
