"""Tests for the identity-keyed, all-tables generalization of case_merge.

Spec odd/tasks/duplicados-por-abogado.md, T2:
- S2: duplicates are grouped by (rol, court_id, procedure), never by ROL alone.
- S3: every table with a FK to cases.id is re-pointed and verified.
- S4: divergent histories are UNIONed onto the winner.

SQLite does not enforce FKs, so "no FK violation after delete" is asserted
directly: no row in any child table references a deleted loser id.
"""

from datetime import date, datetime, timedelta

import pytest

from app.core.database import Base
from app.models.cartera_snapshot import CarteraSnapshot
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.generated_document import GeneratedDocument
from app.models.hito import Hito, HitoTipo
from app.models.lawyer import Lawyer
from app.models.liberacion import LiberacionRequest
from app.models.movement import Movement
from app.services import case_merge
from app.services.case_merge import CaseMergeVerificationError, run_case_merge

FIRM_RUT = "16021492-9"

# case_merge_audit.winner_case_id points at the WINNER, not at a child row.
EXCLUDED_FKS = {("case_merge_audit", "winner_case_id")}


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def carla(db):
    lawyer = Lawyer(rut="22222222-2", name="Carla")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def sandy(db):
    lawyer = Lawyer(rut="11111111-1", name="Sandy")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def court(db):
    c = Court(code="T2-A", name="Juzgado A", region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def other_court(db):
    c = Court(code="T2-B", name="Juzgado B", region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


def _case(db, lawyer, court, rol="C-1-2024", procedure=None):
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


def _child_models():
    return list(case_merge.CHILD_TABLES_WITH_CASE_ID)


def _assert_no_row_references(db, case_ids):
    for model in _child_models():
        assert db.query(model).filter(model.case_id.in_(case_ids)).count() == 0, model.__tablename__


class TestIdentityGrouping:
    def test_same_rol_in_different_courts_is_not_merged(self, db, firm, carla, sandy, court, other_court):
        a = _case(db, carla, court)
        b = _case(db, sandy, other_court)

        result = run_case_merge(db)

        assert result.merged_rol_count == 0
        assert result.losers_deleted == 0
        survivors = {c.id: c for c in db.query(Case).all()}
        assert set(survivors) == {a.id, b.id}
        assert {c.lawyer_id for c in survivors.values()} == {firm.id}

    def test_same_rol_and_court_with_different_procedure_is_not_merged(
        self, db, firm, carla, sandy, court
    ):
        a = _case(db, carla, court, procedure="Principal")
        b = _case(db, sandy, court, procedure="Medida prejudicial")

        result = run_case_merge(db)

        assert result.merged_rol_count == 0
        assert {c.id for c in db.query(Case).all()} == {a.id, b.id}
        assert {c.lawyer_id for c in db.query(Case).all()} == {firm.id}

    def test_none_and_empty_procedure_are_the_same_identity(self, db, firm, carla, sandy, court):
        a = _case(db, carla, court, procedure=None)
        _case(db, sandy, court, procedure="")

        result = run_case_merge(db)

        assert result.merged_rol_count == 1
        assert result.losers_deleted == 1
        assert result.merged_rols == ["C-1-2024"]
        assert db.query(Case).count() == 1
        assert db.query(Case).one().id == a.id

    def test_only_the_matching_identity_inside_a_mixed_rol_merges(
        self, db, firm, carla, sandy, court, other_court
    ):
        a = _case(db, carla, court)
        dup = _case(db, sandy, court)
        other = _case(db, sandy, other_court)
        db.add(_movement(a.id, "1", "x"))
        db.flush()

        result = run_case_merge(db)

        assert result.merged_rol_count == 1
        assert {c.id for c in db.query(Case).all()} == {a.id, other.id}
        assert db.query(Case).filter(Case.id == dup.id).first() is None

    def test_rerun_is_noop_when_distinct_identities_survive(self, db, carla, sandy, court, other_court):
        _case(db, carla, court)
        _case(db, sandy, other_court)
        run_case_merge(db)

        again = run_case_merge(db)

        assert again.merged_rol_count == 0
        assert again.losers_deleted == 0
        assert again.reowned_case_count == 0


class TestVerifyUsesIdentity:
    def test_survivors_sharing_identity_fail_verification(self, db, carla, sandy, court):
        _case(db, carla, court)
        _case(db, sandy, court, procedure="")

        with pytest.raises(CaseMergeVerificationError):
            case_merge._verify_invariants(db, [])

    def test_survivors_sharing_only_rol_pass_verification(self, db, carla, sandy, court, other_court):
        _case(db, carla, court)
        _case(db, sandy, other_court)

        case_merge._verify_invariants(db, [])


class TestAllCaseFkTablesCovered:
    def test_every_table_with_fk_to_cases_is_covered(self):
        expected = set()
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                for fk in column.foreign_keys:
                    if fk.column.table.name == "cases" and (table.name, column.name) not in EXCLUDED_FKS:
                        expected.add((table.name, column.name))

        covered = {(m.__tablename__, "case_id") for m in case_merge.CHILD_TABLES_WITH_CASE_ID}

        assert expected, "metadata introspection found no FK to cases"
        assert expected == covered


class TestNewChildTablesMerged:
    def _tipo(self, db):
        tipo = HitoTipo(code="t", label="t", nivel="junior", valor_bruto=100)
        db.add(tipo)
        db.flush()
        return tipo

    def test_hito_liberacion_generated_document_are_repointed(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "rich"))
        tipo = self._tipo(db)
        db.add_all(
            [
                Hito(
                    lawyer_id=sandy.id,
                    hito_tipo_id=tipo.id,
                    valor_bruto=100,
                    fecha_hito=date(2026, 1, 1),
                    case_id=loser.id,
                ),
                LiberacionRequest(case_id=loser.id, target_semaforo="verde"),
                GeneratedDocument(case_id=loser.id, document_type="escrito_oposicion"),
            ]
        )
        db.flush()
        loser_id, winner_id = loser.id, winner.id

        run_case_merge(db)

        assert db.query(Hito).one().case_id == winner_id
        assert db.query(LiberacionRequest).one().case_id == winner_id
        assert db.query(GeneratedDocument).one().case_id == winner_id
        _assert_no_row_references(db, [loser_id])

    def test_cartera_snapshot_collision_on_periodo_keeps_winner_row(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "rich"))
        db.add_all(
            [
                CarteraSnapshot(periodo="2026-01", case_id=winner.id),
                CarteraSnapshot(periodo="2026-01", case_id=loser.id),
                CarteraSnapshot(periodo="2026-02", case_id=loser.id),
            ]
        )
        db.flush()
        loser_id, winner_id = loser.id, winner.id

        run_case_merge(db)

        rows = db.query(CarteraSnapshot).all()
        assert sorted((r.periodo, r.case_id) for r in rows) == [
            ("2026-01", winner_id),
            ("2026-02", winner_id),
        ]
        _assert_no_row_references(db, [loser_id])

    def test_case_lawyer_source_of_loser_is_merged_without_duplicates(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "rich"))
        now = datetime.utcnow()
        db.add_all(
            [
                CaseLawyerSource(case_id=loser.id, lawyer_id=sandy.id, first_seen_at=now, last_seen_at=now),
                CaseLawyerSource(case_id=loser.id, lawyer_id=carla.id, first_seen_at=now, last_seen_at=now),
            ]
        )
        db.flush()
        loser_id, winner_id = loser.id, winner.id

        run_case_merge(db)

        pairs = sorted((r.case_id, r.lawyer_id) for r in db.query(CaseLawyerSource).all())
        assert pairs == sorted([(winner_id, carla.id), (winner_id, sandy.id)])
        _assert_no_row_references(db, [loser_id])

    def test_verify_detects_stray_row_in_new_table(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(GeneratedDocument(case_id=loser.id, document_type="escrito_oposicion"))
        db.flush()

        with pytest.raises(CaseMergeVerificationError, match="generated_documents"):
            case_merge._verify_invariants(db, [loser.id])
        assert winner.id != loser.id


class TestDivergentHistoriesAreUnioned:
    def test_winner_ends_with_union_of_movements(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add_all(
            [
                _movement(winner.id, "1", "A"),
                _movement(winner.id, "2", "B"),
                _movement(loser.id, "2", "B"),
                _movement(loser.id, "3", "C"),
            ]
        )
        db.flush()
        winner_id, loser_id = winner.id, loser.id

        run_case_merge(db)

        moves = db.query(Movement).filter(Movement.case_id == winner_id).all()
        assert sorted((m.folio, m.description) for m in moves) == [("1", "A"), ("2", "B"), ("3", "C")]
        assert db.query(Movement).filter(Movement.case_id == loser_id).count() == 0


class TestReverseMergeRespectsIdentity:
    def test_reverse_does_not_skip_a_different_causa_of_the_same_lawyer(
        self, db, carla, sandy, court, other_court
    ):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "rich"))
        db.flush()
        run_case_merge(db)
        # Sandy still owns a legitimately different causa with the same ROL.
        _case(db, sandy, other_court)

        recreated = case_merge.reverse_case_merge(db)

        assert recreated == 1
        assert loser.id is not None
