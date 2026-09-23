"""Tests for app/services/cartera_snapshot.py.

"Cartera del mes = snapshot del día 1 a las 00:00 hrs": these tests cover the
owner-resolution precedence used to freeze each row, that nivel and freshness
evidence are truly frozen (never re-joined against live data), idempotency /
reemplazar semantics, the scheduler's take-once-per-period hook, period
comparison with graceful missing-side handling, and the frescura bucket
boundaries + advertencia rule.
"""
from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cartera_snapshot import CarteraSnapshot, CarteraSnapshotRun
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.cartera_snapshot import (
    ADVERTENCIA_FRESCURA,
    _build_frescura,
    _frescura_bucket,
    comparar_periodos,
    frescura_actual,
    periodo_actual,
    snapshot_detalle,
    tomar_snapshot,
)
from app.workers.sync_scheduler import _maybe_take_cartera_snapshot

PROVENANCE_RUT = "10000000-1"
LITIGANTE_RUT = "20000000-2"
ASSIGNED_RUT = "30000000-3"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def court(db):
    obj = Court(code="T1-CARTERA", name="Juzgado Cartera", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def provenance_lawyer(db):
    obj = Lawyer(rut=PROVENANCE_RUT, name="Provenance Lawyer", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def litigante_lawyer(db):
    obj = Lawyer(
        rut=LITIGANTE_RUT, name="Litigante Lawyer", role="lawyer", is_firm_lawyer=True, nivel="junior"
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def assigned_lawyer(db):
    obj = Lawyer(
        rut=ASSIGNED_RUT, name="Assigned Lawyer", role="lawyer", is_firm_lawyer=True, nivel="senior"
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, owner_lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=owner_lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigante(db, case, lawyer):
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=lawyer.rut,
            persona_type="NATURAL",
            nombre=lawyer.name,
            natural_key=f"{case.id}-{lawyer.rut}",
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. Resolved owner precedence: override > litigante > lawyer_id
# ---------------------------------------------------------------------------


def test_snapshot_uses_resolved_owner_not_litigante_nor_lawyer_id(
    db, court, provenance_lawyer, litigante_lawyer, assigned_lawyer
):
    case = _make_case(db, provenance_lawyer, court, "C-1-2026", matriz="M2")
    _seed_litigante(db, case, litigante_lawyer)
    case.assigned_lawyer_id = assigned_lawyer.id
    db.commit()

    tomar_snapshot(db, "2026-09")

    row = db.query(CarteraSnapshot).filter(CarteraSnapshot.case_id == case.id).one()
    assert row.lawyer_id == assigned_lawyer.id
    assert row.lawyer_id != litigante_lawyer.id
    assert row.lawyer_id != provenance_lawyer.id


# ---------------------------------------------------------------------------
# 2. nivel + freshness are frozen at snapshot time
# ---------------------------------------------------------------------------


def test_nivel_and_freshness_are_frozen_on_the_row(db, court, litigante_lawyer):
    old_movement = datetime(2026, 8, 1, 12, 0, 0)
    old_checked = datetime(2026, 8, 15, 9, 0, 0)
    case = _make_case(
        db,
        litigante_lawyer,
        court,
        "C-2-2026",
        matriz="M1 Baja",
        last_movement_at=old_movement,
        last_detail_checked_at=old_checked,
    )
    _seed_litigante(db, case, litigante_lawyer)

    tomar_snapshot(db, "2026-09")

    # Mutate the live data AFTER the snapshot.
    litigante_lawyer.nivel = "pleno"
    case.last_movement_at = datetime(2026, 9, 20, 0, 0, 0)
    case.last_detail_checked_at = datetime(2026, 9, 22, 0, 0, 0)
    db.commit()

    row = db.query(CarteraSnapshot).filter(CarteraSnapshot.case_id == case.id).one()
    assert row.nivel == "junior"  # old value, not "pleno"
    assert row.last_movement_at == old_movement
    assert row.last_detail_checked_at == old_checked


# ---------------------------------------------------------------------------
# 3 & 4. Idempotency + reemplazar
# ---------------------------------------------------------------------------


def test_tomar_snapshot_idempotent_without_reemplazar(db, court, provenance_lawyer):
    _make_case(db, provenance_lawyer, court, "C-3-2026", matriz="M2")

    first = tomar_snapshot(db, "2026-09")
    assert first["causas"] == 1

    # A new case appears in the live table after the first snapshot.
    _make_case(db, provenance_lawyer, court, "C-4-2026", matriz="M3")

    second = tomar_snapshot(db, "2026-09")
    assert second == first  # untouched — same summary returned

    rows = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == "2026-09").all()
    assert len(rows) == 1  # no duplicate rows, new case NOT picked up

    runs = db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == "2026-09").all()
    assert len(runs) == 1


def test_tomar_snapshot_reemplazar_rebuilds(db, court, provenance_lawyer):
    _make_case(db, provenance_lawyer, court, "C-5-2026", matriz="M2")
    tomar_snapshot(db, "2026-09")

    _make_case(db, provenance_lawyer, court, "C-6-2026", matriz="M3")

    rebuilt = tomar_snapshot(db, "2026-09", reemplazar=True)
    assert rebuilt["causas"] == 2

    rows = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == "2026-09").all()
    assert len(rows) == 2

    runs = db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == "2026-09").all()
    assert len(runs) == 1  # rebuilt in place, not appended


# ---------------------------------------------------------------------------
# 5. Scheduler hook
# ---------------------------------------------------------------------------


def test_scheduler_takes_snapshot_when_none_exists(db, court, provenance_lawyer):
    _make_case(db, provenance_lawyer, court, "C-7-2026", matriz="M2")

    current_periodo = periodo_actual()
    assert db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == current_periodo).first() is None

    _maybe_take_cartera_snapshot(db)

    run = db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == current_periodo).first()
    assert run is not None
    assert run.tomado_por is None  # automatic


def test_scheduler_skips_when_run_already_exists(db, court, provenance_lawyer):
    _make_case(db, provenance_lawyer, court, "C-8-2026", matriz="M2")
    _maybe_take_cartera_snapshot(db)

    # A new case appears before the next cycle.
    _make_case(db, provenance_lawyer, court, "C-9-2026", matriz="M3")
    _maybe_take_cartera_snapshot(db)

    current_periodo = periodo_actual()
    runs = db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == current_periodo).all()
    assert len(runs) == 1

    rows = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == current_periodo).all()
    assert len(rows) == 1  # the second cycle did nothing


# ---------------------------------------------------------------------------
# 6. comparar_periodos
# ---------------------------------------------------------------------------


def test_comparar_periodos_deltas_and_missing_side(
    db, court, provenance_lawyer, litigante_lawyer, assigned_lawyer
):
    case_a = _make_case(db, provenance_lawyer, court, "C-10-2026", matriz="M2")
    _seed_litigante(db, case_a, litigante_lawyer)
    tomar_snapshot(db, "2026-08")

    # Between periods: matriz changes, and a NEW lawyer (assigned_lawyer)
    # appears via a fresh reassignment while litigante_lawyer keeps nothing.
    case_a.matriz = "M3"
    case_a.assigned_lawyer_id = assigned_lawyer.id
    db.add(case_a)
    case_b = _make_case(db, provenance_lawyer, court, "C-11-2026", matriz="M1 Baja")
    db.commit()
    tomar_snapshot(db, "2026-09")

    comparison = comparar_periodos(db, "2026-08", "2026-09")

    matriz_by_key = {item["matriz"]: item for item in comparison["por_matriz"]}
    assert matriz_by_key["M2"]["desde"] == 1
    assert matriz_by_key["M2"]["hasta"] == 0
    assert matriz_by_key["M2"]["delta"] == -1
    assert matriz_by_key["M3"]["desde"] == 0
    assert matriz_by_key["M3"]["hasta"] == 1
    assert matriz_by_key["M3"]["delta"] == 1
    assert matriz_by_key["M1 Baja"]["hasta"] == 1

    lawyer_by_id = {item["lawyer_id"]: item for item in comparison["por_abogado"]}
    # litigante_lawyer was the owner in "2026-08" but not in "2026-09"
    # (override replaced it) — must appear with hasta=0, not be dropped.
    assert lawyer_by_id[litigante_lawyer.id]["desde"] == 1
    assert lawyer_by_id[litigante_lawyer.id]["hasta"] == 0
    assert lawyer_by_id[litigante_lawyer.id]["delta"] == -1
    # assigned_lawyer is new in "2026-09" — must appear with desde=0.
    assert lawyer_by_id[assigned_lawyer.id]["desde"] == 0
    assert lawyer_by_id[assigned_lawyer.id]["hasta"] == 1
    assert lawyer_by_id[assigned_lawyer.id]["delta"] == 1


# ---------------------------------------------------------------------------
# 7. Frescura bucket boundaries + advertencia
# ---------------------------------------------------------------------------


def test_frescura_bucket_boundaries():
    now = datetime(2026, 9, 23, 0, 0, 0)

    assert _frescura_bucket(None, reference=now) == "nunca"
    assert _frescura_bucket(now, reference=now) == "ultimos_7d"
    assert _frescura_bucket(now - timedelta(days=7), reference=now) == "ultimos_7d"
    assert _frescura_bucket(now - timedelta(days=7, seconds=1), reference=now) == "entre_7_30d"
    assert _frescura_bucket(now - timedelta(days=30), reference=now) == "entre_7_30d"
    assert _frescura_bucket(now - timedelta(days=30, seconds=1), reference=now) == "entre_30_90d"
    assert _frescura_bucket(now - timedelta(days=90), reference=now) == "entre_30_90d"
    assert _frescura_bucket(now - timedelta(days=90, seconds=1), reference=now) == "mas_90d"


def test_frescura_advertencia_below_50_pct():
    from collections import Counter

    # 4 of 10 checked within 30 days -> 40% < 50%.
    buckets = Counter({"ultimos_7d": 2, "entre_7_30d": 2, "mas_90d": 6})
    block = _build_frescura(buckets, 10)
    assert block["pct_al_dia"] == 40.0
    assert block["advertencia"] == ADVERTENCIA_FRESCURA


def test_frescura_no_advertencia_at_or_above_50_pct():
    from collections import Counter

    # 5 of 10 checked within 30 days -> exactly 50%, not below it.
    buckets = Counter({"ultimos_7d": 5, "mas_90d": 5})
    block = _build_frescura(buckets, 10)
    assert block["pct_al_dia"] == 50.0
    assert block["advertencia"] is None


def test_frescura_actual_live_and_per_abogado(db, court, provenance_lawyer, litigante_lawyer):
    now = datetime.utcnow()
    fresh_case = _make_case(
        db, provenance_lawyer, court, "C-12-2026", last_detail_checked_at=now
    )
    _seed_litigante(db, fresh_case, litigante_lawyer)
    stale_case = _make_case(
        db,
        provenance_lawyer,
        court,
        "C-13-2026",
        last_detail_checked_at=now - timedelta(days=200),
    )
    _seed_litigante(db, stale_case, litigante_lawyer)

    result = frescura_actual(db)
    assert result["total"] == 2
    assert result["buckets"]["ultimos_7d"] == 1
    assert result["buckets"]["mas_90d"] == 1

    lawyer_row = next(r for r in result["por_abogado"] if r["lawyer_id"] == litigante_lawyer.id)
    assert lawyer_row["total"] == 2
