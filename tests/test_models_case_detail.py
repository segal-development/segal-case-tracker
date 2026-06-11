"""Tests for the 4 new case-detail entity models (S1-T08 — RED first).

Verifies:
- All 4 model classes are importable and have the required attributes.
- natural_key column exists on each model.
- UniqueConstraint(case_id, natural_key) is enforced by the DB.
"""

import pytest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

# Side-effect import: registers all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court


# ---------------------------------------------------------------------------
# Shared in-memory SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def sqlite_db():
    """Isolated in-memory SQLite session for each test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="function")
def seeded_case(sqlite_db):
    """Lawyer + Court + Case pre-seeded in the test DB."""
    db = sqlite_db
    lawyer = Lawyer(
        rut="11111111-1",
        name="Test Lawyer",
        email="test@example.com",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()

    court = Court(code="T1-TEST", name="Juzgado Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9999-2025",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    return {"db": db, "case": case}


# ---------------------------------------------------------------------------
# S1-T08a: model attribute tests
# ---------------------------------------------------------------------------


class TestModelAttributes:
    """All 4 models exist and expose a natural_key attribute."""

    def test_case_litigante_has_natural_key(self):
        assert hasattr(CaseLitigante(), "natural_key")

    def test_case_notificacion_has_natural_key(self):
        assert hasattr(CaseNotificacion(), "natural_key")

    def test_case_escrito_has_natural_key(self):
        assert hasattr(CaseEscrito(), "natural_key")

    def test_case_exhorto_has_natural_key(self):
        assert hasattr(CaseExhorto(), "natural_key")


# ---------------------------------------------------------------------------
# S1-T08b: CaseLitigante persistence
# ---------------------------------------------------------------------------


class TestCaseLitigante:
    def test_insert_litigante(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        row = CaseLitigante(
            case_id=case.id,
            participante="DTE.",
            rut="81826800-9",
            persona_type="JURIDICA",
            nombre="CAJA DE COMPENSACION TEST",
            natural_key="81826800-9|dte.",
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        assert row.id is not None

    def test_unique_constraint_prevents_duplicate_natural_key(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        key = "dup-key-litigante"
        db.add(CaseLitigante(
            case_id=case.id, participante="DTE.", rut="1-1",
            persona_type="NATURAL", nombre="A", natural_key=key,
            created_at=datetime.utcnow(),
        ))
        db.commit()

        db.add(CaseLitigante(
            case_id=case.id, participante="DDO.", rut="2-2",
            persona_type="NATURAL", nombre="B", natural_key=key,
            created_at=datetime.utcnow(),
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# S1-T08c: CaseNotificacion persistence
# ---------------------------------------------------------------------------


class TestCaseNotificacion:
    def test_insert_notificacion(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        row = CaseNotificacion(
            case_id=case.id,
            rol="C-9999-2025",
            estado_notif="REALIZADA",
            tipo_notif="PERSONAL",
            tipo_participante="DDO.",
            nombre="FERNANDEZ",
            tramite="Demanda",
            obs_fallida="",
            natural_key="notif-abc-001",
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        assert row.id is not None

    def test_unique_constraint_notificacion(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        key = "dup-notif-key"
        for _ in range(2):
            db.add(CaseNotificacion(
                case_id=case.id, rol="C-1", estado_notif="OK",
                tipo_notif="T", tipo_participante="P", nombre="N",
                tramite="T", obs_fallida="", natural_key=key,
                created_at=datetime.utcnow(),
            ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# S1-T08d: CaseEscrito persistence
# ---------------------------------------------------------------------------


class TestCaseEscrito:
    def test_insert_escrito(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        row = CaseEscrito(
            case_id=case.id,
            tipo_escrito="DEMANDA",
            solicitante="BANCO ITAU",
            tiene_documento=True,
            tiene_anexo=False,
            natural_key="escrito-abc-001",
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        assert row.id is not None

    def test_unique_constraint_escrito(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        key = "dup-escrito-key"
        for _ in range(2):
            db.add(CaseEscrito(
                case_id=case.id, tipo_escrito="T", solicitante="S",
                tiene_documento=False, tiene_anexo=False,
                natural_key=key, created_at=datetime.utcnow(),
            ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------------------
# S1-T08e: CaseExhorto persistence
# ---------------------------------------------------------------------------


class TestCaseExhorto:
    def test_insert_exhorto(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        row = CaseExhorto(
            case_id=case.id,
            rol_origen="C-1253-2015",
            tipo_exhorto="ACTIVO",
            rol_destino="E-355-2026",
            tribunal_destino="JUZGADO DE LETRAS TEST",
            estado="PENDIENTE",
            natural_key="exhorto-abc-001",
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        assert row.id is not None

    def test_unique_constraint_exhorto(self, seeded_case):
        db = seeded_case["db"]
        case = seeded_case["case"]
        key = "dup-exhorto-key"
        for _ in range(2):
            db.add(CaseExhorto(
                case_id=case.id, rol_origen="R1", tipo_exhorto="T",
                rol_destino="R2", tribunal_destino="Court",
                estado="PEND", natural_key=key, created_at=datetime.utcnow(),
            ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
