"""``_upsert_movement`` never stores a movement whose date did not parse.

A corrupted scrape (shifted columns) used to land as a duplicate row with
``movement_date = now`` and a date as its description.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app as _app  # noqa: F401 — registers all models
from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.sync_service import NotifyBudget, ScrapedMovement, SyncService


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def case(db):
    lawyer = Lawyer(
        rut="12345678-9", name="Abogada Test", email="test@segal.cl",
        is_active=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    court = Court(code="C1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-1234-2025",
        competencia="civil", status="active",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    return case


def _movement(fecha="30/10/2025", descripcion="Ingreso demanda", folio="1"):
    return ScrapedMovement(
        folio=folio, fecha=fecha, tipo_tramite="Escrito",
        descripcion=descripcion, etapa="Inicio de la Tramitación",
    )


def _sync(db, case, movements):
    return SyncService(db).sync_movements(
        case.id, movements, budget=NotifyBudget(0)
    )


def _rows(db, case):
    return db.query(Movement).filter(Movement.case_id == case.id).all()


@pytest.mark.parametrize("fecha", ["", "no es fecha", "31/02/2025"])
def test_unparseable_date_is_skipped_never_now(db, case, fecha):
    new_count, _ = _sync(db, case, [_movement(fecha=fecha)])
    assert new_count == 0
    assert _rows(db, case) == []


def test_description_that_is_only_a_date_is_skipped(db, case):
    new_count, _ = _sync(db, case, [_movement(descripcion="30/10/2025")])
    assert new_count == 0
    assert _rows(db, case) == []


def test_skip_logs_warning_with_case_id_and_folio(db, case, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.sync_service"):
        _sync(db, case, [_movement(fecha="", folio="77")])
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"case_id={case.id}" in m and "folio=77" in m for m in messages)


def test_valid_rows_next_to_skipped_ones_are_stored(db, case):
    new_count, _ = _sync(
        db, case,
        [_movement(), _movement(fecha="", folio="2", descripcion="Otro")],
    )
    assert new_count == 1
    (row,) = _rows(db, case)
    assert (row.folio, row.description) == ("1", "Ingreso demanda")
    assert row.movement_date == datetime(2025, 10, 30)


def test_resync_of_same_table_inserts_nothing(db, case):
    table = [_movement(), _movement(folio="2", descripcion="Proveído", fecha="02/11/2025")]
    first, _ = _sync(db, case, table)
    second, _ = _sync(db, case, table)
    assert (first, second) == (2, 0)
    assert len(_rows(db, case)) == 2
