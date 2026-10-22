"""Tests for the read-only duplicate-cases report (T5a of duplicados-por-abogado).

The report must classify losers (EMPTY vs NON-EMPTY), count movements that a
merge would UNION into the winner, pick winners with the same rule as
``case_merge._select_winner`` and do all of it with a FIXED number of SQL
statements, never writing anything.
"""

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from app.core.database import Base
from app.models.cartera_snapshot import CarteraSnapshot
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.document import Document
from app.models.generated_document import GeneratedDocument
from app.models.hito import Hito, HitoTipo
from app.models.lawyer import Lawyer
from app.models.liberacion import LiberacionRequest
from app.models.movement import Movement
from app.services import case_merge
from app.services.dedup_cases_report import (
    QUERIES_PER_LOSER_ESTIMATE,
    ROUND_TRIP_SECONDS,
    build_dedup_report,
)

FIRM_RUT = "16021492-9"
EXCLUDED_FKS = {("case_merge_audit", "winner_case_id")}


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    return lawyer


def _lawyer(db, n):
    lawyer = Lawyer(rut=f"9000000{n}-1", name=f"Lawyer {n}")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def carla(db):
    return _lawyer(db, 1)


@pytest.fixture
def sandy(db):
    return _lawyer(db, 2)


@pytest.fixture
def pablo(db):
    return _lawyer(db, 3)


def _court(db, code):
    court = Court(code=code, name=f"Juzgado {code}", region="RM", type="civil")
    db.add(court)
    db.flush()
    return court


@pytest.fixture
def court(db):
    return _court(db, "T5-A")


@pytest.fixture
def other_court(db):
    return _court(db, "T5-B")


def _case(db, lawyer, court, rol="C-1-2024", procedure=None, competencia="civil"):
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        procedure=procedure,
        competencia=competencia,
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


def _group_for(report, rol):
    matches = [g for g in report.groups if g.rol == rol]
    assert len(matches) == 1, f"expected one group for {rol}, got {len(matches)}"
    return matches[0]


class TestGrouping:
    def test_same_rol_in_two_courts_is_not_a_group(self, db, carla, sandy, court, other_court):
        _case(db, carla, court)
        _case(db, sandy, other_court)

        report = build_dedup_report(db)

        assert report.total_groups == 0
        assert report.surplus_rows == 0
        assert report.groups == []

    def test_principal_and_medida_of_one_rol_is_not_a_group(self, db, carla, sandy, court):
        _case(db, carla, court, procedure="Principal")
        _case(db, sandy, court, procedure="Medida prejudicial")

        report = build_dedup_report(db)

        assert report.total_groups == 0

    def test_none_and_empty_procedure_are_one_group(self, db, carla, sandy, court):
        _case(db, carla, court, procedure=None)
        _case(db, sandy, court, procedure="")

        report = build_dedup_report(db)

        assert report.total_groups == 1
        assert report.surplus_rows == 1

    def test_non_civil_duplicates_are_ignored_like_the_merge(self, db, carla, sandy, court):
        _case(db, carla, court, competencia="laboral")
        _case(db, sandy, court, competencia="laboral")

        assert build_dedup_report(db).total_groups == 0

    def test_totals_and_groups_by_copies(self, db, carla, sandy, pablo, court):
        _case(db, carla, court, rol="C-1-2024")
        _case(db, sandy, court, rol="C-1-2024")
        _case(db, carla, court, rol="C-2-2024")
        _case(db, sandy, court, rol="C-2-2024")
        _case(db, pablo, court, rol="C-2-2024")

        report = build_dedup_report(db)

        assert report.total_groups == 2
        assert report.surplus_rows == 3
        assert report.groups_by_copies == {2: 1, 3: 1}
        assert report.groups_with_three_or_more_copies == 1


class TestWinnerSelection:
    def test_winner_agrees_with_select_winner_on_a_mixed_fixture(self, db, carla, sandy, pablo, court, other_court):
        # Group A: richest copy is NOT the lowest id.
        a1 = _case(db, carla, court, rol="C-A-2024")
        a2 = _case(db, sandy, court, rol="C-A-2024")
        db.add_all([_movement(a2.id, "1", "x"), _movement(a2.id, "2", "y"), _movement(a1.id, "1", "x")])
        # Group B: tie (all empty) -> lowest id.
        _case(db, carla, other_court, rol="C-B-2024")
        _case(db, sandy, other_court, rol="C-B-2024")
        # Group C: documents count towards richness, 3 copies.
        c1 = _case(db, carla, court, rol="C-C-2024")
        c2 = _case(db, sandy, court, rol="C-C-2024")
        _case(db, pablo, court, rol="C-C-2024")
        db.add_all([Document(case_id=c2.id, filename="a.pdf"), _movement(c1.id, "1", "z")])
        db.flush()

        report = build_dedup_report(db)

        assert report.total_groups == 3
        for group in report.groups:
            cases = (
                db.query(Case)
                .filter(Case.id.in_([group.winner_id, *group.loser_ids]))
                .order_by(Case.id)
                .all()
            )
            assert group.winner_id == case_merge._select_winner(db, cases).id
        assert _group_for(report, "C-A-2024").winner_id == a2.id

    def test_winner_changing_owner_is_flagged(self, db, firm, carla, sandy, court):
        # Winner owned by Carla (richest); loser owned by the firm -> owner changes.
        winner = _case(db, carla, court, rol="C-1-2024")
        _case(db, firm, court, rol="C-1-2024")
        db.add(_movement(winner.id, "1", "x"))
        # Winner already firm-owned -> no owner change.
        _case(db, firm, court, rol="C-2-2024")
        _case(db, sandy, court, rol="C-2-2024")
        db.flush()

        report = build_dedup_report(db)

        assert _group_for(report, "C-1-2024").winner_changes_owner is True
        assert _group_for(report, "C-2-2024").winner_changes_owner is False
        assert report.groups_winner_changes_owner == 1


class TestLoserClassification:
    def test_empty_vs_non_empty_losers(self, db, carla, sandy, pablo, court):
        winner = _case(db, carla, court)
        empty = _case(db, sandy, court)
        non_empty = _case(db, pablo, court)
        db.add_all([_movement(winner.id, "1", "x"), _movement(non_empty.id, "9", "only-here")])
        db.flush()

        report = build_dedup_report(db)

        group = _group_for(report, "C-1-2024")
        assert group.winner_id == winner.id
        assert group.empty_loser_ids == [empty.id]
        assert group.non_empty_loser_ids == [non_empty.id]
        assert report.empty_losers == 1
        assert report.non_empty_losers == 1
        assert report.empty_only_first_pass_rows == 1

    def test_a_loser_with_only_a_hito_is_not_empty(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "x"))
        tipo = HitoTipo(code="t", label="t", nivel="junior", valor_bruto=100)
        db.add(tipo)
        db.flush()
        db.add(
            Hito(
                lawyer_id=sandy.id, hito_tipo_id=tipo.id, valor_bruto=100,
                fecha_hito=date(2026, 1, 1), case_id=loser.id,
            )
        )
        db.flush()

        report = build_dedup_report(db)

        assert report.empty_losers == 0
        assert _group_for(report, "C-1-2024").non_empty_loser_ids == [loser.id]
        assert report.loser_rows_by_table["hitos"] == 1
        assert report.hitos_on_losers_by_estado == {"pendiente": 1}

    def test_case_lawyer_source_only_loser_is_a_separate_tier(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "x"))
        now = datetime.utcnow()
        db.add(CaseLawyerSource(case_id=loser.id, lawyer_id=sandy.id, first_seen_at=now, last_seen_at=now))
        db.flush()

        report = build_dedup_report(db)

        group = _group_for(report, "C-1-2024")
        assert group.empty_loser_ids == []
        assert group.source_only_loser_ids == [loser.id]
        assert group.non_empty_loser_ids == []
        assert report.empty_losers == 0
        assert report.source_only_losers == 1
        assert report.non_empty_losers == 0
        assert report.loser_rows_by_table["case_lawyer_source"] == 1

    def test_rows_attached_to_losers_are_counted_per_table(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "x"))
        db.add_all(
            [
                LiberacionRequest(case_id=loser.id, target_semaforo="verde"),
                GeneratedDocument(case_id=loser.id, document_type="escrito_oposicion"),
                CarteraSnapshot(periodo="2026-01", case_id=loser.id),
                CarteraSnapshot(periodo="2026-02", case_id=loser.id),
                # Rows on the winner are NOT counted.
                CarteraSnapshot(periodo="2026-01", case_id=winner.id),
            ]
        )
        db.flush()

        report = build_dedup_report(db)

        assert report.loser_rows_by_table["liberacion_requests"] == 1
        assert report.loser_rows_by_table["generated_documents"] == 1
        assert report.loser_rows_by_table["cartera_snapshots"] == 2
        assert report.loser_rows_by_table["movements"] == 0

    def test_every_child_table_is_accounted_for(self, db, carla, sandy, court):
        _case(db, carla, court)
        _case(db, sandy, court)

        report = build_dedup_report(db)

        expected = {m.__tablename__ for m in case_merge.CHILD_TABLES_WITH_CASE_ID}
        assert set(report.loser_rows_by_table) == expected

    def test_child_table_list_matches_the_metadata(self):
        expected = set()
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                for fk in column.foreign_keys:
                    if fk.column.table.name == "cases" and (table.name, column.name) not in EXCLUDED_FKS:
                        expected.add(table.name)

        from app.services.dedup_cases_report import child_tables

        assert expected, "metadata introspection found no FK to cases"
        assert {m.__tablename__ for m in child_tables()} == expected


class TestMovementDivergence:
    def test_contained_vs_unioned_movements(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add_all(
            [
                _movement(winner.id, "1", "A"),
                _movement(winner.id, "2", "B"),
                _movement(winner.id, "3", "C"),
                _movement(loser.id, "2", "B"),  # contained
                _movement(loser.id, "4", "D"),  # unioned
                _movement(loser.id, "5", "E"),  # unioned
            ]
        )
        db.flush()

        report = build_dedup_report(db)

        group = _group_for(report, "C-1-2024")
        assert group.winner_id == winner.id
        assert group.movements_contained == 1
        assert group.movements_unioned == 2
        assert group.is_diverging is True
        assert report.movements_contained_total == 1
        assert report.movements_unioned_total == 2
        assert report.diverging_groups == 1

    def test_subset_group_is_not_diverging(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add_all([_movement(winner.id, "1", "A"), _movement(winner.id, "2", "B"), _movement(loser.id, "1", "A")])
        db.flush()

        report = build_dedup_report(db)

        group = _group_for(report, "C-1-2024")
        assert group.movements_contained == 1
        assert group.movements_unioned == 0
        assert group.is_diverging is False
        assert report.diverging_groups == 0

    def test_two_losers_with_the_same_new_movement_union_it_once(self, db, carla, sandy, pablo, court):
        winner = _case(db, carla, court)
        l1 = _case(db, sandy, court)
        l2 = _case(db, pablo, court)
        db.add_all(
            [
                _movement(winner.id, "1", "A"),
                _movement(winner.id, "2", "B"),
                _movement(l1.id, "9", "NEW"),
                _movement(l2.id, "9", "NEW"),
            ]
        )
        db.flush()

        group = _group_for(build_dedup_report(db), "C-1-2024")

        assert group.movements_unioned == 1
        assert group.movements_contained == 1


class TestRiskFlags:
    def test_identity_violations_are_zero_by_construction(self, db, carla, sandy, court):
        _case(db, carla, court)
        _case(db, sandy, court)

        report = build_dedup_report(db)

        assert report.groups_spanning_court_or_competencia == 0


class TestEstimates:
    def test_estimate_formula_is_explicit(self, db, carla, sandy, pablo, court):
        _case(db, carla, court)
        _case(db, sandy, court)
        _case(db, pablo, court)

        report = build_dedup_report(db)

        est = report.estimate
        assert est["queries_per_loser"] == QUERIES_PER_LOSER_ESTIMATE == 40
        assert est["round_trip_seconds"] == ROUND_TRIP_SECONDS == 0.135
        assert est["losers"] == 2
        assert est["hours_all_losers"] == pytest.approx(2 * 40 * 0.135 / 3600)
        assert "40" in est["formula"] and "0.135" in est["formula"] and "3600" in est["formula"]
        # Both losers are strictly empty: nothing is left for the merge after pass 1.
        assert est["losers_after_empty_pass"] == 0
        assert est["hours_after_empty_pass"] == 0


class TestToDict:
    def test_to_dict_is_json_serializable(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add_all([_movement(winner.id, "1", "A"), _movement(loser.id, "2", "B")])
        db.flush()

        payload = build_dedup_report(db).to_dict()

        decoded = json.loads(json.dumps(payload))
        assert decoded["totals"]["groups"] == 1
        assert decoded["groups"][0]["movements_unioned"] == 1


class TestReadOnlyAndFixedCost:
    def _count_statements(self, db, fn):
        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _record)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        return statements

    def _populate(self, db, groups, court, lawyers):
        for n in range(groups):
            rol = f"C-{n}-2024"
            winner = _case(db, lawyers[0], court, rol=rol)
            db.add(_movement(winner.id, "1", "A"))
            for lawyer in lawyers[1:]:
                loser = _case(db, lawyer, court, rol=rol)
                db.add(_movement(loser.id, "2", f"B{n}"))
        db.flush()

    def test_statement_count_does_not_scale_with_groups(self, db, carla, sandy, pablo, court):
        self._populate(db, 2, court, [carla, sandy])
        small = self._count_statements(db, lambda: build_dedup_report(db))

        for n in range(2, 14):
            rol = f"C-{n}-2024"
            first = _case(db, carla, court, rol=rol)
            db.add(_movement(first.id, "1", "A"))
            for lawyer in (sandy, pablo):
                loser = _case(db, lawyer, court, rol=rol)
                db.add(_movement(loser.id, "2", f"B{n}"))
        db.flush()
        large = self._count_statements(db, lambda: build_dedup_report(db))

        assert len(small) > 0
        assert len(small) == len(large)
        assert len(large) <= 20

    def test_report_never_calls_run_case_merge(self, db, monkeypatch, carla, sandy, court):
        _case(db, carla, court)
        _case(db, sandy, court)

        def _boom(*args, **kwargs):
            raise AssertionError("the report must never run the merge")

        monkeypatch.setattr(case_merge, "run_case_merge", _boom)

        assert build_dedup_report(db).total_groups == 1

    def test_report_leaves_database_untouched_and_pending_state_unflushed(self, db, carla, sandy, court):
        winner = _case(db, carla, court)
        loser = _case(db, sandy, court)
        db.add(_movement(winner.id, "1", "A"))
        db.add(_movement(loser.id, "2", "B"))
        db.flush()
        pending = Case(lawyer_id=carla.id, court_id=court.id, rol="C-PENDING-2024", competencia="civil")
        db.add(pending)

        def counts():
            return {
                t.name: db.execute(select(func.count()).select_from(t)).scalar()
                for t in Base.metadata.sorted_tables
            }

        before = counts()
        statements = self._count_statements(db, lambda: build_dedup_report(db))
        after = counts()

        assert before == after
        assert pending in db.new  # still pending, never flushed by the report
        assert not db.dirty and not db.deleted
        writes = [s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        assert writes == []
