"""Tests for app.services.case_merge (ADR-008 Slice 2 data migration pipeline).

Merges duplicate Case rows (same ROL, ingested under different lawyers
pre-Approach-C) into one firm-owned Case: winner = richest (most
movements+documents+litigantes, lowest id tie-break), children re-pointed and
deduped by natural key, case_lawyer_source backfilled, invariants verified
BEFORE any Case delete, losers deleted, survivors re-owned to the firm id.

Pure/DB-session pipeline — no commits inside; tests call db.commit() (or
inspect pre-commit state) themselves, mirroring how the real Alembic
migration (024) will manage the transaction.
"""

from datetime import datetime, timedelta

import pytest

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.case_litigante import CaseLitigante
from app.models.case_merge_audit import CaseMergeAudit
from app.models.case_notificacion import CaseNotificacion
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services import case_merge
from app.services.case_merge import CaseMergeVerificationError, run_case_merge

FIRM_RUT = "16021492-9"


@pytest.fixture(autouse=True)
def firm(db):
    lawyer = Lawyer(rut=FIRM_RUT, name="Firm Lawyer")
    db.add(lawyer)
    db.flush()
    return lawyer


@pytest.fixture
def court(db):
    c = Court(code="T1-MERGE", name="Juzgado Merge Test", region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


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
def marcela(db):
    lawyer = Lawyer(rut="33333333-3", name="Marcela")
    db.add(lawyer)
    db.flush()
    return lawyer


def _movement(case_id, folio, description, days_ago=1):
    return Movement(
        case_id=case_id,
        folio=folio,
        description=description,
        movement_date=datetime.utcnow() - timedelta(days=days_ago),
    )


def _litigante(case_id, participante, rut, nombre, natural_key):
    return CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=natural_key,
    )


@pytest.fixture
def duplicate_rol_scenario(db, court, carla, sandy, marcela):
    """ROL C-1234-2024 exists under Carla (richest -> winner) and Sandy.

    Carla: 5 movements, 2 litigantes.
    Sandy: 3 movements (1 overlapping natural key with Carla, 2 distinct), 1 litigante.
    Mirrors the spec's "Duplicate ROL across two lawyers merges without data
    loss" scenario: 7 distinct surviving movements (5+3-1 dup), 3 distinct
    litigantes (2+1, no overlap).
    """
    winner = Case(lawyer_id=carla.id, court_id=court.id, rol="C-1234-2024", competencia="civil")
    loser = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-1234-2024", competencia="civil")
    db.add_all([winner, loser])
    db.flush()

    # Carla (winner): 5 movements, 2 litigantes.
    for i in range(4):
        db.add(_movement(winner.id, f"W-{i}", f"Winner movement {i}", days_ago=i + 1))
    # This one is the "overlapping" movement — same (case_id-independent) folio+description as Sandy's dup.
    db.add(_movement(winner.id, "SHARED", "Shared movement text"))
    db.add(_litigante(winner.id, "DTE", carla.rut, "Carla", "carla-dte"))
    db.add(_litigante(winner.id, "AB.DTE", carla.rut, "Carla", "carla-ab-dte"))

    # Sandy (loser): 3 movements (1 true dup by folio+description, 2 distinct), 1 litigante.
    db.add(_movement(loser.id, "SHARED", "Shared movement text"))  # true duplicate vs winner's
    db.add(_movement(loser.id, "S-1", "Sandy distinct movement 1"))
    db.add(_movement(loser.id, "S-2", "Sandy distinct movement 2"))
    db.add(_litigante(loser.id, "AB.DDO", sandy.rut, "Sandy", "sandy-ab-ddo"))

    db.flush()
    return {
        "winner": winner,
        "loser": loser,
        "carla": carla,
        "sandy": sandy,
    }


class TestWinnerSelection:
    def test_richest_case_wins(self, db, duplicate_rol_scenario):
        result = run_case_merge(db)

        winner_id = duplicate_rol_scenario["winner"].id
        loser_id = duplicate_rol_scenario["loser"].id

        assert db.query(Case).filter(Case.id == winner_id).first() is not None
        assert db.query(Case).filter(Case.id == loser_id).first() is None
        assert result.merged_rol_count == 1
        assert result.losers_deleted == 1

    def test_tie_break_is_lowest_id(self, db, court, carla, sandy):
        """Equal richness -> the lower-id Case wins."""
        # Create winner-candidate (lower id) first, then an equally-rich case.
        first = Case(lawyer_id=carla.id, court_id=court.id, rol="C-TIE-2024", competencia="civil")
        db.add(first)
        db.flush()
        second = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-TIE-2024", competencia="civil")
        db.add(second)
        db.flush()
        # Identical richness: 1 movement each, 0 documents, 0 litigantes.
        db.add(_movement(first.id, "F-1", "Movement"))
        db.add(_movement(second.id, "F-2", "Movement"))
        db.flush()

        run_case_merge(db)

        assert db.query(Case).filter(Case.id == first.id).first() is not None
        assert db.query(Case).filter(Case.id == second.id).first() is None


class TestChildRepointAndDedupe:
    def test_children_repointed_and_natural_key_deduped(self, db, duplicate_rol_scenario):
        winner = duplicate_rol_scenario["winner"]

        run_case_merge(db)

        movements = db.query(Movement).filter(Movement.case_id == winner.id).all()
        # 5 winner + 3 loser - 1 true dup (folio+description "SHARED") = 7
        assert len(movements) == 7
        keys = {(m.folio, m.description) for m in movements}
        assert len(keys) == 7  # no duplicate natural keys remain

        litigantes = db.query(CaseLitigante).filter(CaseLitigante.case_id == winner.id).all()
        assert len(litigantes) == 3
        assert {l.natural_key for l in litigantes} == {
            "carla-dte",
            "carla-ab-dte",
            "sandy-ab-ddo",
        }

    def test_no_orphaned_children_after_merge(self, db, duplicate_rol_scenario):
        loser_id = duplicate_rol_scenario["loser"].id

        run_case_merge(db)

        assert db.query(Movement).filter(Movement.case_id == loser_id).count() == 0
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == loser_id).count() == 0

    def test_document_movement_fk_repointed_when_movement_deduped(self, db, court, carla, sandy):
        """A Document pointing at a movement that gets deduped away must be
        re-pointed to the surviving canonical movement, not orphaned."""
        winner = Case(lawyer_id=carla.id, court_id=court.id, rol="C-DOC-2024", competencia="civil")
        loser = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-DOC-2024", competencia="civil")
        db.add_all([winner, loser])
        db.flush()

        winner_movement = _movement(winner.id, "F-1", "Same movement text")
        # Two extra distinct movements make `winner` unambiguously richer
        # than `loser` even after `loser` gets its own document below
        # (richness = movements + documents + litigantes).
        db.add(_movement(winner.id, "F-2", "Winner-only movement 2"))
        db.add(_movement(winner.id, "F-3", "Winner-only movement 3"))
        loser_movement = _movement(loser.id, "F-1", "Same movement text")
        db.add_all([winner_movement, loser_movement])
        db.flush()

        doc = Document(case_id=loser.id, movement_id=loser_movement.id, filename="a.pdf")
        db.add(doc)
        db.flush()
        doc_id = doc.id
        loser_movement_id = loser_movement.id

        run_case_merge(db)

        db.refresh(doc) if db.query(Document).filter(Document.id == doc_id).first() else None
        refreshed_doc = db.query(Document).filter(Document.id == doc_id).first()
        assert refreshed_doc is not None
        assert refreshed_doc.case_id == winner.id
        assert refreshed_doc.movement_id == winner_movement.id
        assert refreshed_doc.movement_id != loser_movement_id
        # The deduped-away loser movement itself must be gone.
        assert db.query(Movement).filter(Movement.id == loser_movement_id).first() is None


class TestCaseLawyerSourceBackfill:
    def test_backfills_source_for_every_pre_merge_lawyer_id(self, db, duplicate_rol_scenario):
        winner = duplicate_rol_scenario["winner"]
        carla = duplicate_rol_scenario["carla"]
        sandy = duplicate_rol_scenario["sandy"]

        run_case_merge(db)

        sources = db.query(CaseLawyerSource).filter(CaseLawyerSource.case_id == winner.id).all()
        source_lawyer_ids = {s.lawyer_id for s in sources}
        assert carla.id in source_lawyer_ids
        assert sandy.id in source_lawyer_ids


class TestAuditTrail:
    def test_audit_row_recorded_before_delete(self, db, duplicate_rol_scenario):
        winner = duplicate_rol_scenario["winner"]
        loser = duplicate_rol_scenario["loser"]
        loser_id = loser.id
        sandy = duplicate_rol_scenario["sandy"]

        run_case_merge(db)

        audit = db.query(CaseMergeAudit).filter(CaseMergeAudit.loser_case_id == loser_id).first()
        assert audit is not None
        assert audit.winner_case_id == winner.id
        assert audit.loser_lawyer_id == sandy.id
        assert audit.rol == "C-1234-2024"


class TestReOwnership:
    def test_survivors_reowned_to_firm_id(self, db, firm, duplicate_rol_scenario):
        winner = duplicate_rol_scenario["winner"]

        run_case_merge(db)

        db.refresh(winner)
        assert winner.lawyer_id == firm.id


class TestIdempotency:
    def test_rerun_after_merge_is_a_noop(self, db, firm, duplicate_rol_scenario):
        winner = duplicate_rol_scenario["winner"]

        first_result = run_case_merge(db)
        assert first_result.losers_deleted == 1

        movements_after_first = db.query(Movement).filter(Movement.case_id == winner.id).count()
        litigantes_after_first = db.query(CaseLitigante).filter(CaseLitigante.case_id == winner.id).count()

        second_result = run_case_merge(db)

        assert second_result.merged_rol_count == 0
        assert second_result.losers_deleted == 0
        assert db.query(Movement).filter(Movement.case_id == winner.id).count() == movements_after_first
        assert (
            db.query(CaseLitigante).filter(CaseLitigante.case_id == winner.id).count()
            == litigantes_after_first
        )
        db.refresh(winner)
        assert winner.lawyer_id == firm.id


class TestVerifyGate:
    def test_verify_failure_raises_and_does_not_delete(self, db, duplicate_rol_scenario, monkeypatch):
        loser_id = duplicate_rol_scenario["loser"].id

        def _boom(db_arg, loser_ids):
            raise CaseMergeVerificationError("forced failure for test")

        monkeypatch.setattr(case_merge, "_verify_invariants", _boom)

        with pytest.raises(CaseMergeVerificationError):
            run_case_merge(db)

        # Nothing was deleted: the loser Case row must still exist.
        assert db.query(Case).filter(Case.id == loser_id).first() is not None

    def test_verify_detects_orphaned_child_row(self, db, court, carla, sandy):
        """Direct unit test of the invariant-detection logic: a child row
        still referencing a to-be-deleted loser case_id must be caught."""
        winner = Case(lawyer_id=carla.id, court_id=court.id, rol="C-ORPHAN-2024", competencia="civil")
        loser = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-ORPHAN-2024", competencia="civil")
        db.add_all([winner, loser])
        db.flush()
        # Simulate a repoint bug: a movement still pointing at the loser.
        db.add(_movement(loser.id, "F-1", "Not repointed"))
        db.flush()

        with pytest.raises(CaseMergeVerificationError):
            case_merge._verify_invariants(db, [loser.id])

        assert db.query(Case).filter(Case.id == loser.id).first() is not None


class TestNoDataLossAcrossAllEightTables:
    def test_all_eight_child_tables_repointed_and_deduped(self, db, court, carla, sandy):
        winner = Case(lawyer_id=carla.id, court_id=court.id, rol="C-ALL8-2024", competencia="civil")
        loser = Case(lawyer_id=sandy.id, court_id=court.id, rol="C-ALL8-2024", competencia="civil")
        db.add_all([winner, loser])
        db.flush()

        db.add(_movement(winner.id, "M-W", "movement w"))
        db.add(_movement(loser.id, "M-L", "movement l"))
        db.add(Document(case_id=winner.id, filename="w.pdf"))
        db.add(Document(case_id=loser.id, filename="l.pdf"))
        db.add(_litigante(winner.id, "DTE", carla.rut, "Carla", "w-lit"))
        db.add(_litigante(loser.id, "AB.DDO", sandy.rut, "Sandy", "l-lit"))
        db.add(
            CaseNotificacion(
                case_id=winner.id,
                rol="C-ALL8-2024",
                estado_notif="ok",
                tipo_notif="tipo",
                tipo_participante="DTE",
                nombre="Carla",
                tramite="tramite",
                natural_key="w-notif",
            )
        )
        db.add(
            CaseNotificacion(
                case_id=loser.id,
                rol="C-ALL8-2024",
                estado_notif="ok",
                tipo_notif="tipo",
                tipo_participante="DDO",
                nombre="Sandy",
                tramite="tramite",
                natural_key="l-notif",
            )
        )
        db.add(
            CaseEscrito(
                case_id=winner.id,
                tipo_escrito="tipo",
                solicitante="Carla",
                natural_key="w-escrito",
            )
        )
        db.add(
            CaseEscrito(
                case_id=loser.id,
                tipo_escrito="tipo",
                solicitante="Sandy",
                natural_key="l-escrito",
            )
        )
        db.add(
            CaseExhorto(
                case_id=winner.id,
                rol_origen="C-ALL8-2024",
                tipo_exhorto="ACTIVO",
                rol_destino="E-1-2026",
                tribunal_destino="Tribunal X",
                estado="ok",
                natural_key="w-exhorto",
            )
        )
        db.add(
            CaseExhorto(
                case_id=loser.id,
                rol_origen="C-ALL8-2024",
                tipo_exhorto="ACTIVO",
                rol_destino="E-2-2026",
                tribunal_destino="Tribunal Y",
                estado="ok",
                natural_key="l-exhorto",
            )
        )
        db.add(
            CaseDeadline(
                case_id=winner.id,
                deadline_type="contestacion",
                due_date=datetime.utcnow().date(),
                triggered_at=datetime.utcnow().date(),
            )
        )
        db.add(
            CaseDeadline(
                case_id=loser.id,
                deadline_type="replica",
                due_date=datetime.utcnow().date(),
                triggered_at=datetime.utcnow().date(),
            )
        )
        db.add(Alert(lawyer_id=carla.id, case_id=winner.id, type="new_movement", title="w-alert"))
        db.add(Alert(lawyer_id=sandy.id, case_id=loser.id, type="new_movement", title="l-alert"))
        db.flush()

        run_case_merge(db)

        assert db.query(Movement).filter(Movement.case_id == winner.id).count() == 2
        assert db.query(Document).filter(Document.case_id == winner.id).count() == 2
        assert db.query(CaseLitigante).filter(CaseLitigante.case_id == winner.id).count() == 2
        assert db.query(CaseNotificacion).filter(CaseNotificacion.case_id == winner.id).count() == 2
        assert db.query(CaseEscrito).filter(CaseEscrito.case_id == winner.id).count() == 2
        assert db.query(CaseExhorto).filter(CaseExhorto.case_id == winner.id).count() == 2
        assert db.query(CaseDeadline).filter(CaseDeadline.case_id == winner.id).count() == 2
        assert db.query(Alert).filter(Alert.case_id == winner.id).count() == 2

        for model in (
            Movement,
            Document,
            CaseLitigante,
            CaseNotificacion,
            CaseEscrito,
            CaseExhorto,
            CaseDeadline,
            Alert,
        ):
            assert db.query(model).filter(model.case_id == loser.id).count() == 0
