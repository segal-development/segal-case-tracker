"""Tests for scripts/recalcular_matriz.py's testable core (compute_distribution).

Uses the injected ``db`` fixture (real SQLite, per conftest) — never touches
``app.core.database.SessionLocal`` / a real DB, exactly the point of
separating ``compute_distribution`` (session injected) from ``main`` (owns
the real session).
"""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo
from app.models.movement import Movement
from scripts.recalcular_matriz import compute_distribution


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-RECALC", name="Juzgado Recalc", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active",
        competencia="civil", created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def one_mapped_case(db, lawyer, court):
    db.add(MatrizPjudMapeo(pjud_stage="Ingreso", matriz_etapa="ASIGNACIÓN"))
    db.add(MatrizClasificacion(
        proc_simple="Juicio Ejecutivo Completo", proc_antiguo="JUICIO EJECUTIVO",
        etapa="ASIGNACIÓN", matriz="M1 Baja",
    ))
    db.commit()
    case = _make_case(db, lawyer, court, "C-1-2026")
    db.add(Movement(
        case_id=case.id, stage="Ingreso", description="mov", movement_date=datetime(2026, 1, 1),
    ))
    db.commit()
    return case


class TestDryRun:
    def test_dry_run_writes_nothing(self, db, one_mapped_case):
        result = compute_distribution(db, dry_run=True)

        assert result["total"] == 1
        assert result["by_matriz"]["M1 Baja"] == 1

        db.expire_all()
        refreshed = db.query(Case).filter(Case.id == one_mapped_case.id).first()
        assert refreshed.matriz is None
        assert refreshed.matriz_origen is None
        assert refreshed.matriz_computed_at is None

    def test_normal_run_persists(self, db, one_mapped_case):
        result = compute_distribution(db, dry_run=False)

        assert result["by_matriz"]["M1 Baja"] == 1

        db.expire_all()
        refreshed = db.query(Case).filter(Case.id == one_mapped_case.id).first()
        assert refreshed.matriz == "M1 Baja"
        assert refreshed.matriz_origen == "pjud_etapa"
        assert refreshed.matriz_computed_at is not None

    def test_dry_run_reports_no_mapeada_ids(self, db, lawyer, court):
        case = _make_case(db, lawyer, court, "C-2-2026")
        db.add(Movement(
            case_id=case.id, stage="Etapa Rara", description="mov",
            movement_date=datetime(2026, 1, 1),
        ))
        db.commit()

        result = compute_distribution(db, dry_run=True)

        assert result["no_mapeada_ids"] == [case.id]
        assert result["by_origen"]["no_mapeada"] == 1

        db.expire_all()
        refreshed = db.query(Case).filter(Case.id == case.id).first()
        assert refreshed.matriz_origen is None  # untouched by dry-run


class TestSysgalDistribution:
    def _linked(self, db, lawyer, court, one_mapped_case):
        from app.models.sysgal_causa import SysgalCausa
        db.add(MatrizClasificacion(
            proc_simple="Juicio Ejecutivo Completo", proc_antiguo="JUICIO EJECUTIVO",
            etapa="EXHIBICIÓN DOCUMENTOS", matriz="M2",
        ))
        db.add(SysgalCausa(sysgal_id=1, rol="C-1-2026", rol_norm="C-1-2026", tribunal="x",
                           etapa="EXHIBICIÓN DOCUMENTOS", estado="activa", case_id=one_mapped_case.id))
        db.commit()

    def test_dry_run_reports_before_after_and_origen_without_writing(self, db, lawyer, court, one_mapped_case):
        self._linked(db, lawyer, court, one_mapped_case)
        one_mapped_case.matriz = "M1 Baja"
        db.commit()

        result = compute_distribution(db, dry_run=True)

        assert result["before_by_matriz"]["M1 Baja"] == 1
        assert result["by_matriz"]["M2"] == 1
        assert result["by_origen"]["sysgal_etapa"] == 1
        db.expire_all()
        assert db.get(Case, one_mapped_case.id).matriz == "M1 Baja"

    def test_print_distribution_shows_before_and_after(self, capsys):
        from collections import Counter
        from scripts.recalcular_matriz import _print_distribution
        _print_distribution(Counter({"M2": 1}), Counter({"sysgal_etapa": 1}), 1, Counter({"M1 Baja": 1}))
        out = capsys.readouterr().out
        assert "antes" in out and "después" in out and "sysgal_etapa" in out


class TestTerminadaEnSysgal:
    def _terminada(self, db, case, sid=1, estado="terminada"):
        from app.models.sysgal_causa import SysgalCausa
        db.add(SysgalCausa(sysgal_id=sid, rol="C-1-2026", rol_norm="C-1-2026", tribunal="x",
                           etapa="X", estado=estado, case_id=case.id))
        db.commit()

    def test_apply_sets_flag_and_reports_count(self, db, one_mapped_case):
        self._terminada(db, one_mapped_case)

        result = compute_distribution(db, dry_run=False)

        db.expire_all()
        assert db.get(Case, one_mapped_case.id).terminada_en_sysgal is True
        assert result["terminadas_sysgal"] == 1 and result["before_terminadas_sysgal"] == 0

    def test_dry_run_reports_count_without_writing(self, db, one_mapped_case):
        self._terminada(db, one_mapped_case)

        result = compute_distribution(db, dry_run=True)

        db.expire_all()
        assert db.get(Case, one_mapped_case.id).terminada_en_sysgal is False
        assert result["terminadas_sysgal"] == 1

    def test_apply_clears_a_stale_flag_and_case_without_sysgal_is_false(self, db, one_mapped_case):
        one_mapped_case.terminada_en_sysgal = True
        db.commit()

        result = compute_distribution(db, dry_run=False)

        db.expire_all()
        assert db.get(Case, one_mapped_case.id).terminada_en_sysgal is False
        assert result["terminadas_sysgal"] == 0 and result["before_terminadas_sysgal"] == 1

    def test_print_distribution_shows_terminadas(self, capsys):
        from collections import Counter
        from scripts.recalcular_matriz import _print_distribution
        _print_distribution(Counter({"M2": 1}), Counter({"x": 1}), 1, terminadas=(0, 7))
        assert "Terminada en Sysgal" in capsys.readouterr().out
