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
    """Pure-function tests for _compute_prescripcion (no DB needed).

    New rule: the clock starts from Case.filed_at (a DateTime), the plazo is
    always 1 year, and prescription ONLY applies to pagaré/letra/cheque.
    """

    def _case(self, titulo_tipo=None, filed_at=None):
        """Create a minimal mock case with just the relevant input fields."""
        return types.SimpleNamespace(titulo_tipo=titulo_tipo, filed_at=filed_at)

    def _filed(self, **delta):
        """A datetime filed_at relative to TODAY (filed_at is a DateTime)."""
        d = TODAY - relativedelta(**delta)
        return datetime(d.year, d.month, d.day)

    def test_pagare_filed_2_years_ago_is_cumplida(self) -> None:
        """Pagaré filed 2 years ago → cumplida=True, fecha = filed_at + 1yr."""
        from app.services.deadline_engine import _compute_prescripcion

        filed_at = self._filed(years=2)
        case = self._case(titulo_tipo="pagare", filed_at=filed_at)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == filed_at.date() + relativedelta(years=1)

    def test_pagare_filed_6_months_ago_not_cumplida(self) -> None:
        """Pagaré filed 6 months ago → cumplida=False (within 1-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        filed_at = self._filed(months=6)
        case = self._case(titulo_tipo="pagare", filed_at=filed_at)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha == filed_at.date() + relativedelta(years=1)

    def test_letra_filed_13_months_ago_is_cumplida(self) -> None:
        """Letra filed 13 months ago → cumplida=True (past 1-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        filed_at = self._filed(months=13)
        case = self._case(titulo_tipo="letra", filed_at=filed_at)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == filed_at.date() + relativedelta(years=1)

    def test_cheque_filed_13_months_ago_is_cumplida(self) -> None:
        """Cheque filed 13 months ago → cumplida=True (past 1-year plazo)."""
        from app.services.deadline_engine import _compute_prescripcion

        filed_at = self._filed(months=13)
        case = self._case(titulo_tipo="cheque", filed_at=filed_at)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is True
        assert prescripcion_fecha == filed_at.date() + relativedelta(years=1)

    def test_escritura_publica_no_prescription(self) -> None:
        """Escritura pública filed 5 years ago → (False, None): no prescription."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo="escritura_publica", filed_at=self._filed(years=5))
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_sentencia_no_prescription(self) -> None:
        """Sentencia filed 5 years ago → (False, None): no prescription."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo="sentencia", filed_at=self._filed(years=5))
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_otro_no_prescription(self) -> None:
        """Tipo 'otro' filed 5 years ago → (False, None): no prescription."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo="otro", filed_at=self._filed(years=5))
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_none_tipo_no_prescription(self) -> None:
        """titulo_tipo=None filed 5 years ago → (False, None): no prescription."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo=None, filed_at=self._filed(years=5))
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_pagare_filed_at_none_returns_false_none(self) -> None:
        """Pagaré but filed_at=None → (False, None) — not computable."""
        from app.services.deadline_engine import _compute_prescripcion

        case = self._case(titulo_tipo="pagare", filed_at=None)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert cumplida is False
        assert prescripcion_fecha is None

    def test_boundary_exactly_1_year_ago_not_cumplida(self) -> None:
        """Pagaré filed exactly 1 year ago → prescripcion_fecha == today → cumplida=False (strictly <)."""
        from app.services.deadline_engine import _compute_prescripcion

        filed_at = self._filed(years=1)
        case = self._case(titulo_tipo="pagare", filed_at=filed_at)
        cumplida, prescripcion_fecha = _compute_prescripcion(case, TODAY)
        assert prescripcion_fecha == TODAY  # boundary check: fecha == today
        assert cumplida is False  # strictly less than, NOT <=


# ---------------------------------------------------------------------------
# Integration tests — DeadlineEngine.recompute_case sets prescripcion columns
# ---------------------------------------------------------------------------


class TestPrescripcionRecompute:
    """Integration: recompute_case writes prescripcion_cumplida and prescripcion_fecha."""

    def _filed(self, **delta):
        d = TODAY - relativedelta(**delta)
        return datetime(d.year, d.month, d.day)

    def test_pagare_filed_2_years_ago_sets_cumplida_true(self, db) -> None:
        """Pagaré filed 2 years ago → prescripcion_cumplida=True, fecha=filed+1yr."""
        from app.services.deadline_engine import DeadlineEngine

        filed_at = self._filed(years=2)
        case = _make_case(db, titulo_tipo="pagare", filed_at=filed_at)
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is True
        assert case.prescripcion_fecha == filed_at.date() + relativedelta(years=1)

    def test_escritura_no_prescription_after_recompute(self, db) -> None:
        """Escritura pública filed 5 years ago → (False, None) after recompute."""
        from app.services.deadline_engine import DeadlineEngine

        case = _make_case(
            db, titulo_tipo="escritura_publica", filed_at=self._filed(years=5)
        )
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is False
        assert case.prescripcion_fecha is None

    def test_pagare_filed_at_none_sets_cumplida_false(self, db) -> None:
        """Pagaré with filed_at=None → prescripcion_cumplida=False, fecha=None."""
        from app.services.deadline_engine import DeadlineEngine

        case = _make_case(db, titulo_tipo="pagare", filed_at=None)
        # filed_at default in _make_case is a datetime; force None explicitly.
        case.filed_at = None
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is False
        assert case.prescripcion_fecha is None
