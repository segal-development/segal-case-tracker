"""``_upsert_movement`` heals hand-repaired rows in place.

A repaired row has ``movement_date`` right but the date text as its
description. Re-reading the case must restore the real text on that row
instead of inserting a duplicate and alerting on old history.
"""

import logging
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.main import app as _app  # noqa: F401 — registers all models
from app.models.alert import Alert
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.sync_service import NotifyBudget, ScrapedMovement, SyncService

DATE = datetime(2025, 10, 30)


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


def _scraped(fecha="30/10/2025", descripcion="Ingreso demanda", folio="1"):
    return ScrapedMovement(
        folio=folio, fecha=fecha, tipo_tramite="Escrito",
        descripcion=descripcion, etapa="Inicio de la Tramitación",
    )


def _repaired(db, case, folio="1", date=DATE, description=None):
    row = Movement(
        case_id=case.id, folio=folio, stage="old stage", procedure="old proc",
        description=description or date.strftime("%d/%m/%Y"),
        movement_date=date, created_at=datetime(2026, 1, 1),
    )
    db.add(row)
    db.commit()
    return row


def _rows(db, case):
    return db.query(Movement).filter(Movement.case_id == case.id).all()


def _sync(db, case, movements):
    return SyncService(db).sync_movements(case.id, movements, budget=NotifyBudget(0))


@pytest.fixture(params=[True, False], ids=["with_map", "without_map"])
def existing_map(request, db, case):
    """Build the prefetch map for the test, or None to force the DB path."""
    def build():
        if not request.param:
            return None
        return {(m.folio, m.description): m for m in _rows(db, case)}
    return build


def test_repaired_row_is_healed_in_place(db, case, existing_map):
    row = _repaired(db, case)
    row_id, created_at = row.id, row.created_at
    mapping = existing_map()

    movement, is_new = SyncService(db)._upsert_movement(
        case.id, _scraped(), existing_by_key=mapping
    )

    assert is_new is False
    assert movement.id == row_id
    (only,) = _rows(db, case)
    assert only.id == row_id
    assert only.description == "Ingreso demanda"
    assert only.stage == "Inicio de la Tramitación"
    assert only.procedure == "Escrito"
    assert only.movement_date == DATE
    assert only.created_at == created_at
    if mapping is not None:
        assert ("1", "30/10/2025") not in mapping
        assert mapping[("1", "Ingreso demanda")] is only


def test_heal_creates_no_alert_nor_notification_and_no_count(db, case):
    _repaired(db, case)
    with patch("app.services.sync_service.NotificationService") as svc_cls:
        new_count, alert_count = SyncService(db).sync_movements(
            case.id, [_scraped()], budget=NotifyBudget(10)
        )
    assert (new_count, alert_count) == (0, 0)
    svc_cls.assert_not_called()
    assert db.query(Alert).count() == 0
    assert len(_rows(db, case)) == 1


@pytest.mark.parametrize("mode", ["with_map", "without_map"])
def test_different_date_is_inserted_not_healed(db, case, mode):
    _repaired(db, case, date=datetime(2025, 10, 29))
    mapping = (
        {(m.folio, m.description): m for m in _rows(db, case)}
        if mode == "with_map" else None
    )
    movement, is_new = SyncService(db)._upsert_movement(
        case.id, _scraped(), existing_by_key=mapping
    )
    assert is_new is True
    assert len(_rows(db, case)) == 2
    assert sorted(m.description for m in _rows(db, case)) == [
        "29/10/2025", "Ingreso demanda",
    ]


def test_two_candidates_with_same_folio_are_not_healed(db, case):
    # Duplicate repaired rows can only coexist in the DB (the prefetch map is
    # keyed by (folio, description) and would collapse them), so this is the
    # no-map path.
    _repaired(db, case)
    _repaired(db, case)
    _, is_new = SyncService(db)._upsert_movement(case.id, _scraped())
    assert is_new is True
    assert len(_rows(db, case)) == 3
    assert sum(m.description == "30/10/2025" for m in _rows(db, case)) == 2


def test_other_folio_repaired_row_is_not_healed(db, case):
    _repaired(db, case, folio="2")
    _, is_new = SyncService(db)._upsert_movement(case.id, _scraped(folio="1"))
    assert is_new is True
    assert len(_rows(db, case)) == 2


def test_normal_row_is_untouched(db, case):
    normal = Movement(
        case_id=case.id, folio="1", stage="S", procedure="P",
        description="Texto real", movement_date=DATE, created_at=datetime(2026, 1, 1),
    )
    db.add(normal)
    db.commit()
    _, is_new = SyncService(db)._upsert_movement(
        case.id, _scraped(descripcion="Ingreso demanda")
    )
    assert is_new is True
    db.refresh(normal)
    assert (normal.description, normal.stage, normal.procedure) == ("Texto real", "S", "P")
    assert len(_rows(db, case)) == 2


def test_healing_twice_is_idempotent(db, case):
    row = _repaired(db, case)
    first = _sync(db, case, [_scraped()])
    second = _sync(db, case, [_scraped()])
    assert (first[0], second[0]) == (0, 0)
    (only,) = _rows(db, case)
    assert only.id == row.id
    assert only.description == "Ingreso demanda"


def test_documents_and_alerts_stay_linked(db, case):
    row = _repaired(db, case)
    doc = Document(case_id=case.id, movement_id=row.id, status="pending")
    alert = Alert(
        lawyer_id=case.lawyer_id, case_id=case.id, movement_id=row.id,
        type="new_movement", title="t", created_at=datetime.utcnow(),
    )
    db.add_all([doc, alert])
    db.commit()

    _sync(db, case, [_scraped()])

    db.refresh(doc)
    db.refresh(alert)
    assert doc.movement_id == row.id
    assert alert.movement_id == row.id
    assert db.query(Alert).count() == 1


def test_heal_logs_one_info_line(db, case, caplog):
    _repaired(db, case)
    with caplog.at_level(logging.INFO, logger="app.services.sync_service"):
        _sync(db, case, [_scraped()])
    healed = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("healed repaired movement")
    ]
    assert healed == [
        f"healed repaired movement case_id={case.id} folio=1 "
        "description='Ingreso demanda'"
    ]
