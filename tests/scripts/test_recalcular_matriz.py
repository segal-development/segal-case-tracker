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
