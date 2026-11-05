"""``Case.last_movement_at`` must equal the newest movement the case HAS.

It is derived data, but it used to be written from whatever a single scrape
happened to see, and only from that list's FIRST element::

    if scraped_movements:
        latest_date = self._parse_date(scraped_movements[0].fecha)
        if latest_date and (not case.last_movement_at or latest_date > case.last_movement_at):
            case.last_movement_at = latest_date

That makes the column depend on the order PJUD returns rows in, and on this
one scrape having seen everything. ``Movement`` rows are written from several
paths (ingest, detail sync, consulta), and only this one touched the column,
so it drifted: measured on QA, 2.113 active cases carried a ``last_movement_at``
OLDER than their own newest movement row.

That is not cosmetic. The column is a decision input, not a label:
``deadline_engine`` compares it against a semaforo override, ``poda`` uses it
to decide what is prunable, and ``lawyer_roster`` uses it to decide what is
stale. A stale value there corrupts decisions.

So these tests do not pin the write path. They pin the INVARIANT: after a
sync, the column equals ``max(movement_date)`` of the case's rows, whatever
produced them and in whatever order they arrived.
"""

from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all.
from app.main import app as _app  # noqa: F401 — side-effect import

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.sync_service import NotifyBudget, ScrapedMovement, SyncService


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
        rut="12345678-9", name="Abogada Test", email="test@segal.cl", is_active=True,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()
    court = Court(code="C1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add(court)
    db.flush()
    row = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-1234-2026", competencia="civil",
        status="active", created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def _mv(folio: str, fecha: str) -> ScrapedMovement:
    return ScrapedMovement(
        folio=folio, fecha=fecha, tipo_tramite="Resolución",
        descripcion=f"Movimiento {folio}", etapa="Cuaderno Principal",
    )


def _sync(db, case, movements):
    SyncService(db).sync_movements(case.id, movements, budget=NotifyBudget(0))
    db.refresh(case)


def _db_max(db, case) -> datetime:
    return (
        db.query(Movement.movement_date)
        .filter(Movement.case_id == case.id)
        .order_by(Movement.movement_date.desc())
        .first()[0]
    )


def test_the_newest_movement_wins_even_when_it_is_not_first_in_the_list(db, case) -> None:
    """``_load_other_cuadernos`` appends another cuaderno's rows AFTER the
    principal ones, so the newest movement is routinely not at index 0."""
    _sync(db, case, [_mv("1", "01/09/2026"), _mv("2", "05/09/2026"), _mv("3", "20/10/2026")])

    assert case.last_movement_at == datetime(2026, 10, 20)
    assert case.last_movement_at == _db_max(db, case)


def test_it_matches_rows_this_scrape_never_saw(db, case) -> None:
    """The invariant that closes the class: several paths insert movements and
    only one used to touch the column. The column must describe the ROWS, not
    the last list someone happened to pass in."""
    db.add(Movement(
        case_id=case.id, folio="9", description="Escrito resuelto",
        movement_date=datetime(2026, 10, 15), created_at=datetime.utcnow(),
    ))
    db.commit()

    _sync(db, case, [_mv("1", "01/09/2026")])

    assert case.last_movement_at == datetime(2026, 10, 15)
    assert case.last_movement_at == _db_max(db, case)


def test_a_rescrape_that_returns_only_old_rows_does_not_pull_the_column_back(db, case) -> None:
    _sync(db, case, [_mv("1", "01/09/2026"), _mv("2", "20/10/2026")])
    assert case.last_movement_at == datetime(2026, 10, 20)

    _sync(db, case, [_mv("1", "01/09/2026")])

    assert case.last_movement_at == datetime(2026, 10, 20)


def test_a_scrape_with_no_movements_leaves_the_column_alone(db, case) -> None:
    _sync(db, case, [_mv("1", "20/10/2026")])
    _sync(db, case, [])

    assert case.last_movement_at == datetime(2026, 10, 20)
