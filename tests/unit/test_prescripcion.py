"""Unit tests for _compute_prescripcion and prescripción integration.

Tests cover the pure function _compute_prescripcion (statute-of-limitations
advisory signal) and the integration path via DeadlineEngine.recompute_case.
"""

from __future__ import annotations

import types
from datetime import date, datetime

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

# Register all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TODAY = date(2026, 6, 16)


# ---------------------------------------------------------------------------
# In-memory SQLite engine (same pattern as test_deadline_engine.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """Pin the engine's clock to TODAY for deterministic assertions."""
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


def _make_case(
    db,
    *,
    competencia: str = "civil",
    filed_at: datetime | None = None,
    last_movement_at: datetime | None = None,
    titulo_tipo: str | None = None,
    titulo_fecha: date | None = None,
) -> Case:
    lawyer = Lawyer(
        rut="22222222-2",
        email="prescripcion@test.com",
        name="Prescripcion Test Lawyer",
    )
    db.add(lawyer)
    court = Court(name="Juzgado Civil P", code="P001", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="P-0001-2026",
        competencia=competencia,
        filed_at=filed_at or datetime(2024, 1, 1),
        last_movement_at=last_movement_at,
        titulo_tipo=titulo_tipo,
        titulo_fecha=titulo_fecha,
    )
    db.add(case)
    db.flush()
    return case


# ---------------------------------------------------------------------------
# Pure function tests — _compute_prescripcion
# ---------------------------------------------------------------------------


class TestComputePrescripcion:
    """Pure-function tests for _compute_prescripcion (no DB needed)."""

    def _case(self, titulo_tipo=None, titulo_fecha=None):
        """Create a minimal mock case with just the two input fields."""
        return types.SimpleNamespace(titulo_tipo=titulo_tipo, titulo_fecha=titulo_fecha)

    def test_pagare_2_years_ago_is_cumplida(self) -> None:
        """Pagaré with titulo_fecha 2 years ago → cumplida=True (1-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=2)
        case = self._case(titulo_tipo="pagare", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=1)

    def test_pagare_6_months_ago_not_cumplida(self) -> None:
        """Pagaré 6 months ago → cumplida=False (still within 1-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(months=6)
        case = self._case(titulo_tipo="pagare", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False

    def test_escritura_publica_4_years_ago_is_cumplida(self) -> None:
        """Escritura pública 4 years ago → cumplida=True (3-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=4)
        case = self._case(titulo_tipo="escritura_publica", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=3)

    def test_escritura_publica_2_years_ago_not_cumplida(self) -> None:
        """Escritura pública 2 years ago → cumplida=False (still within 3-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=2)
        case = self._case(titulo_tipo="escritura_publica", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False

    def test_titulo_fecha_none_returns_false_none(self) -> None:
        """titulo_fecha=None → (False, None) — not computable."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo="pagare", titulo_fecha=None)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_cheque_exactly_1_year_1_day_ago_is_cumplida(self) -> None:
        """Cheque with titulo_fecha exactly 1 year + 1 day ago → cumplida=True."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=1) - relativedelta(days=1)
        case = self._case(titulo_tipo="cheque", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True

    def test_cheque_exactly_1_year_ago_boundary_not_cumplida(self) -> None:
        """Cheque with titulo_fecha exactly 1 year ago → prescripcion_fecha == today → cumplida=False (strictly <)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=1)
        case = self._case(titulo_tipo="cheque", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        expected_fecha = titulo_fecha + relativedelta(years=1)
        assert expected_fecha == TODAY  # boundary check: fecha == today
        assert cumplida is False  # strictly less than, NOT <=

    def test_letra_uses_1_year_plazo(self) -> None:
        """Letra uses 1-year plazo (same as pagaré)."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=2)
        case = self._case(titulo_tipo="letra", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=1)

    def test_otro_uses_3_year_plazo(self) -> None:
        """Tipo 'otro' uses 3-year plazo."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=4)
        case = self._case(titulo_tipo="otro", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=3)

    def test_sentencia_uses_3_year_plazo(self) -> None:
        """Tipo 'sentencia' uses 3-year plazo."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=4)
        case = self._case(titulo_tipo="sentencia", titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=3)

    def test_none_tipo_with_fecha_uses_3_year_plazo(self) -> None:
        """titulo_tipo=None with titulo_fecha set → treated as 'other' → 3-year plazo."""
        from app.services.deadline_engine import _compute_prescripcion

        titulo_fecha = TODAY - relativedelta(years=4)
        case = self._case(titulo_tipo=None, titulo_fecha=titulo_fecha)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == titulo_fecha + relativedelta(years=3)


# ---------------------------------------------------------------------------
# Integration tests — DeadlineEngine.recompute_case sets prescripcion columns
# ---------------------------------------------------------------------------


class TestPrescripcionRecompute:
    """Integration: recompute_case writes prescripcion_cumplida and prescripcion_fecha."""

    def test_pagare_2_years_ago_sets_cumplida_true(self, db) -> None:
        """Pagaré titulo_fecha 2 years ago → prescripcion_cumplida=True after recompute."""
        from app.services.deadline_engine import DeadlineEngine

        titulo_fecha = TODAY - relativedelta(years=2)
        case = _make_case(db, titulo_tipo="pagare", titulo_fecha=titulo_fecha)
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is True
        expected_fecha = titulo_fecha + relativedelta(years=1)
        assert case.prescripcion_fecha == expected_fecha

    def test_titulo_fecha_none_sets_cumplida_false(self, db) -> None:
        """titulo_fecha=None → prescripcion_cumplida=False, prescripcion_fecha=None after recompute."""
        from app.services.deadline_engine import DeadlineEngine

        case = _make_case(db, titulo_tipo=None, titulo_fecha=None)
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is False
        assert case.prescripcion_fecha is None
