"""Cartera snapshot service — freezes the monthly portfolio for tramitación KPIs.

The firm's operating model is "Cartera del mes = snapshot del día 1 a las
00:00 hrs": every tramitación KPI must be computed against a FROZEN monthly
portfolio, never against whatever ``cases`` currently looks like — otherwise
a mid-month reassignment or a nivel change would silently rewrite last
month's numbers.

``tomar_snapshot`` takes that snapshot: one row per civil causa, freezing the
RESOLVED owner (``app.services.lawyer_roster.resolved_owner_by_case`` —
asignación-por-nivel override > litigante of record > nothing; NEVER
``Case.lawyer_id``, which is scraping provenance only), the owner's
``nivel``, the matriz fields, and freshness evidence (``last_movement_at`` /
``last_detail_checked_at``) exactly as they stood at snapshot time.

Freshness evidence travels WITH every row precisely because PJUD-detail
coverage is uneven across the portfolio (see the "sin_detalle" origen in
``app.services.matriz_classifier``): a large share of causas are stale or
never checked. Without it, an activity KPI computed off a snapshot could
look confident while actually resting on stale or missing data — see
``frescura_actual`` / ``GET /cartera/frescura`` for the live counterpart that
every reader of an activity KPI should see alongside it.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.cartera_snapshot import CarteraSnapshot, CarteraSnapshotRun
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import resolved_owner_by_case

logger = logging.getLogger(__name__)

FRESCURA_BUCKETS = ("nunca", "ultimos_7d", "entre_7_30d", "entre_30_90d", "mas_90d")

ADVERTENCIA_FRESCURA = (
    "Menos de la mitad de la cartera tiene el detalle del PJUD al día. Las "
    "métricas de actividad que se calculen sobre estos datos van a "
    "subestimar el trabajo real."
)

#: Rows per INSERT batch. Keeps each statement well inside Postgres' parameter
#: limit while still collapsing thousands of round-trips into a handful.
_INSERT_CHUNK = 1000
_SIN_MATRIZ = "sin_matriz"
_SIN_NIVEL = "sin_nivel"


def periodo_actual(now: Optional[datetime] = None) -> str:
    """Current period in ``YYYY-MM`` form."""
    now = now or datetime.utcnow()
    return f"{now.year:04d}-{now.month:02d}"


def _frescura_bucket(last_checked: Optional[datetime], *, reference: datetime) -> str:
    """Bucket a ``last_detail_checked_at`` value against ``reference``.

    Boundaries: nunca (never checked) / últimos_7d / entre_7_30d /
    entre_30_90d / más_90d. Inclusive on the upper edge of each window (a
    check exactly 7 days old counts as "últimos_7d", not "entre_7_30d").
    """
    if last_checked is None:
        return "nunca"
    delta = reference - last_checked
    if delta <= timedelta(days=7):
        return "ultimos_7d"
    if delta <= timedelta(days=30):
        return "entre_7_30d"
    if delta <= timedelta(days=90):
        return "entre_30_90d"
    return "mas_90d"


def _build_frescura(buckets: Counter, total: int) -> dict:
    """Shape a bucket counter into the ``frescura`` response block.

    ``pct_al_dia`` = share of the total checked within the last 30 days
    (``ultimos_7d`` + ``entre_7_30d``). ``advertencia`` is set (neutral
    Spanish, tuteo) only when that share is below 50%, else ``None`` —
    callers must never show an activity KPI without this context.
    """
    al_dia = buckets.get("ultimos_7d", 0) + buckets.get("entre_7_30d", 0)
    pct_al_dia = round((al_dia / total * 100), 2) if total else 0.0
    return {
        "total": total,
        "buckets": {b: buckets.get(b, 0) for b in FRESCURA_BUCKETS},
        "pct_al_dia": pct_al_dia,
        "advertencia": ADVERTENCIA_FRESCURA if pct_al_dia < 50 else None,
    }


def _run_summary(db: Session, run: CarteraSnapshotRun) -> dict:
    """Rebuild the same summary shape ``tomar_snapshot`` returns, from an
    already-taken run's rows — used by the idempotent no-op path."""
    rows = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == run.periodo).all()

    por_matriz = Counter(r.matriz or _SIN_MATRIZ for r in rows)
    por_nivel = Counter(r.nivel or _SIN_NIVEL for r in rows)
    frescura_buckets = Counter(
        _frescura_bucket(r.last_detail_checked_at, reference=run.tomado_at) for r in rows
    )

    return {
        "periodo": run.periodo,
        "causas": run.causas,
        "tomado_at": run.tomado_at,
        "tomado_por": run.tomado_por,
        "por_matriz": dict(por_matriz),
        "por_nivel": dict(por_nivel),
        "frescura": _build_frescura(frescura_buckets, len(rows)),
    }


def tomar_snapshot(
    db: Session,
    periodo: Optional[str] = None,
    *,
    tomado_por: Optional[str] = None,
    reemplazar: bool = False,
) -> dict:
    """Take (or return the existing) snapshot for ``periodo`` (default: current).

    Idempotent: if a run already exists for ``periodo`` and ``reemplazar`` is
    False, the existing run's summary is returned UNTOUCHED — no rows are
    duplicated and nothing is recomputed. With ``reemplazar=True``, that
    period's ``cartera_snapshots`` rows (and its run) are deleted first, then
    rebuilt from scratch.
    """
    periodo = periodo or periodo_actual()

    existing_run = (
        db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == periodo).first()
    )
    if existing_run is not None and not reemplazar:
        return _run_summary(db, existing_run)

    if existing_run is not None and reemplazar:
        db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == periodo).delete(
            synchronize_session=False
        )
        db.delete(existing_run)
        db.flush()

    now = datetime.utcnow()

    # Single bounded pass over the civil portfolio (~14.5k causas) — no
    # per-case query. Owner resolution reuses the SAME preload pattern the
    # matriz engine and /asignacion endpoints already rely on:
    # ``resolved_owner_by_case`` does its own bounded (not per-case) queries
    # internally, so calling it once here keeps this a fixed number of
    # round-trips regardless of portfolio size.
    cases = (
        db.query(
            Case.id,
            Case.matriz,
            Case.matriz_etapa,
            Case.matriz_origen,
            Case.last_movement_at,
            Case.last_detail_checked_at,
        )
        .filter(Case.competencia == "civil")
        .all()
    )
    owners = resolved_owner_by_case(db, competencia="civil")

    por_matriz: Counter = Counter()
    por_nivel: Counter = Counter()
    frescura_buckets: Counter = Counter()
    # Plain dicts, not ORM instances: a Core insert of mappings lets SQLAlchemy
    # batch them into multi-VALUES statements (insertmanyvalues), while
    # bulk_save_objects falls back to psycopg2's executemany, which sends one
    # INSERT per row. Over the Cloud SQL proxy that turned a 14.5k-row snapshot
    # into ~12 minutes of round-trips.
    rows: list[dict] = []

    for c in cases:
        owner = owners.get(c.id)
        rows.append(
            {
                "periodo": periodo,
                "case_id": c.id,
                "lawyer_id": owner.id if owner is not None else None,
                "nivel": owner.nivel if owner is not None else None,
                "matriz": c.matriz,
                "matriz_etapa": c.matriz_etapa,
                "matriz_origen": c.matriz_origen,
                "last_movement_at": c.last_movement_at,
                "last_detail_checked_at": c.last_detail_checked_at,
                "created_at": now,
            }
        )
        por_matriz[c.matriz or _SIN_MATRIZ] += 1
        por_nivel[(owner.nivel if owner is not None and owner.nivel else _SIN_NIVEL)] += 1
        frescura_buckets[_frescura_bucket(c.last_detail_checked_at, reference=now)] += 1

    if rows:
        for start in range(0, len(rows), _INSERT_CHUNK):
            db.execute(insert(CarteraSnapshot), rows[start : start + _INSERT_CHUNK])

    run = CarteraSnapshotRun(
        periodo=periodo,
        tomado_at=now,
        causas=len(cases),
        tomado_por=tomado_por,
    )
    db.add(run)
    db.commit()

    logger.info(
        "cartera_snapshot: tomado periodo=%s causas=%d tomado_por=%s",
        periodo,
        len(cases),
        tomado_por or "automatico",
    )

    return {
        "periodo": periodo,
        "causas": len(cases),
        "tomado_at": now,
        "tomado_por": tomado_por,
        "por_matriz": dict(por_matriz),
        "por_nivel": dict(por_nivel),
        "frescura": _build_frescura(frescura_buckets, len(cases)),
    }


def snapshot_run(db: Session, periodo: str) -> Optional[CarteraSnapshotRun]:
    """The run row for ``periodo``, or ``None`` if never taken."""
    return db.query(CarteraSnapshotRun).filter(CarteraSnapshotRun.periodo == periodo).first()


def snapshot_detalle(db: Session, periodo: str) -> Optional[dict]:
    """Full per-matriz + per-abogado breakdown for an already-taken period.

    Returns ``None`` when no run exists for ``periodo`` (caller turns that
    into a 404).
    """
    run = snapshot_run(db, periodo)
    if run is None:
        return None

    rows = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == periodo).all()
    total = len(rows)

    por_matriz_counts = Counter(r.matriz or _SIN_MATRIZ for r in rows)
    por_matriz = [
        {
            "matriz": None if key == _SIN_MATRIZ else key,
            "causas": causas,
            "pct": round((causas / total * 100), 2) if total else 0.0,
        }
        for key, causas in sorted(por_matriz_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    by_lawyer_rows: dict[int, list[CarteraSnapshot]] = defaultdict(list)
    for r in rows:
        if r.lawyer_id is not None:
            by_lawyer_rows[r.lawyer_id].append(r)

    lawyer_ids = list(by_lawyer_rows.keys())
    lawyers = (
        {lw.id: lw for lw in db.query(Lawyer).filter(Lawyer.id.in_(lawyer_ids)).all()}
        if lawyer_ids
        else {}
    )

    por_abogado = []
    for lawyer_id, lawyer_rows in by_lawyer_rows.items():
        lawyer_matriz_counts = Counter(r.matriz or _SIN_MATRIZ for r in lawyer_rows)
        lawyer = lawyers.get(lawyer_id)
        por_abogado.append(
            {
                "lawyer_id": lawyer_id,
                "nombre": lawyer.name if lawyer is not None else "",
                # nivel is FROZEN on the snapshot rows — every row for the
                # same lawyer shares it (nivel does not change mid-snapshot),
                # so the first row's value is authoritative for the period.
                "nivel": lawyer_rows[0].nivel,
                "causas": len(lawyer_rows),
                "por_matriz": {
                    (None if k == _SIN_MATRIZ else k): v for k, v in lawyer_matriz_counts.items()
                },
            }
        )
    por_abogado.sort(key=lambda item: (-item["causas"], item["nombre"]))

    frescura_buckets = Counter(
        _frescura_bucket(r.last_detail_checked_at, reference=run.tomado_at) for r in rows
    )

    return {
        "periodo": periodo,
        "tomado_at": run.tomado_at,
        "causas": run.causas,
        "por_matriz": por_matriz,
        "por_abogado": por_abogado,
        "frescura": _build_frescura(frescura_buckets, total),
    }


def comparar_periodos(db: Session, desde: str, hasta: str) -> dict:
    """Per-matriz and per-abogado deltas between two already-taken periods.

    A lawyer (or matriz) present in only one of the two periods is included
    with ``0`` on the missing side — never dropped or raising.
    """
    rows_desde = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == desde).all()
    rows_hasta = db.query(CarteraSnapshot).filter(CarteraSnapshot.periodo == hasta).all()

    matriz_desde = Counter(r.matriz or _SIN_MATRIZ for r in rows_desde)
    matriz_hasta = Counter(r.matriz or _SIN_MATRIZ for r in rows_hasta)
    matriz_keys = sorted(set(matriz_desde) | set(matriz_hasta))
    por_matriz = [
        {
            "matriz": None if key == _SIN_MATRIZ else key,
            "desde": matriz_desde.get(key, 0),
            "hasta": matriz_hasta.get(key, 0),
            "delta": matriz_hasta.get(key, 0) - matriz_desde.get(key, 0),
        }
        for key in matriz_keys
    ]

    lawyer_desde = Counter(r.lawyer_id for r in rows_desde if r.lawyer_id is not None)
    lawyer_hasta = Counter(r.lawyer_id for r in rows_hasta if r.lawyer_id is not None)
    lawyer_ids = sorted(set(lawyer_desde) | set(lawyer_hasta))
    lawyers = (
        {lw.id: lw.name for lw in db.query(Lawyer).filter(Lawyer.id.in_(lawyer_ids)).all()}
        if lawyer_ids
        else {}
    )
    por_abogado = [
        {
            "lawyer_id": lawyer_id,
            "nombre": lawyers.get(lawyer_id, ""),
            "desde": lawyer_desde.get(lawyer_id, 0),
            "hasta": lawyer_hasta.get(lawyer_id, 0),
            "delta": lawyer_hasta.get(lawyer_id, 0) - lawyer_desde.get(lawyer_id, 0),
        }
        for lawyer_id in lawyer_ids
    ]

    return {
        "desde": desde,
        "hasta": hasta,
        "por_matriz": por_matriz,
        "por_abogado": por_abogado,
    }


def frescura_actual(db: Session) -> dict:
    """Live freshness of the current civil portfolio — NOT a snapshot read.

    Every activity KPI drawn from a snapshot should be shown alongside this
    (or the snapshot's own frozen ``frescura`` block) so the reader knows how
    stale the underlying PJUD data is. Overall + per resolved-owner lawyer.
    """
    now = datetime.utcnow()

    cases = (
        db.query(Case.id, Case.last_detail_checked_at)
        .filter(Case.competencia == "civil")
        .all()
    )
    owners = resolved_owner_by_case(db, competencia="civil")

    total = len(cases)
    buckets: Counter = Counter()
    por_lawyer_buckets: dict[int, Counter] = defaultdict(Counter)
    por_lawyer_total: Counter = Counter()

    for c in cases:
        bucket = _frescura_bucket(c.last_detail_checked_at, reference=now)
        buckets[bucket] += 1
        owner = owners.get(c.id)
        if owner is not None:
            por_lawyer_buckets[owner.id][bucket] += 1
            por_lawyer_total[owner.id] += 1

    result = _build_frescura(buckets, total)

    lawyer_ids = list(por_lawyer_buckets.keys())
    lawyer_names = (
        {lw.id: lw.name for lw in db.query(Lawyer).filter(Lawyer.id.in_(lawyer_ids)).all()}
        if lawyer_ids
        else {}
    )

    por_abogado = []
    for lawyer_id, lawyer_buckets in por_lawyer_buckets.items():
        entry = _build_frescura(lawyer_buckets, por_lawyer_total[lawyer_id])
        entry["lawyer_id"] = lawyer_id
        entry["nombre"] = lawyer_names.get(lawyer_id, "")
        por_abogado.append(entry)
    por_abogado.sort(key=lambda item: item["lawyer_id"])

    result["por_abogado"] = por_abogado
    return result
