"""Batched, resumable runner for the duplicate-``cases`` cleanup (T5b2).

Thin orchestration over ``run_case_merge(db, group_limit=N)``: one NEW session
and one transaction per batch, committed only if the merge returned cleanly.
Built so that running it by mistake is hard:

* ``apply=False`` (default) writes nothing and never calls ``run_case_merge``.
* ``apply=True`` needs ``backup_ref`` (the verified backup) and, unless
  ``require_quiescent=False``, a quiescent database. ``assert_quiescent`` is
  the code behind the runbook's "stop the worker": it refuses when a movement
  or a sync_history row was written within ``quiescence_s`` seconds.
* The first error of any kind rolls back THAT batch only and stops the run.
  There is no retry and no skipping ahead: re-running resumes from the same
  group because merged groups stop being duplicates.
* A batch that merges nothing while groups remain stops the run (no progress).

After the last executed batch, and every ``verify_every`` batches, a fixed
number of aggregate queries (one per table of ``CHILD_TABLES_WITH_CASE_ID``)
checks that no child row points at a missing case; any orphan stops the run.
"""

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.movement import Movement
from app.models.sync_history import SyncHistory
from app.services.case_merge import (
    CHILD_TABLES_WITH_CASE_ID,
    _select_batch_identities,
    count_duplicate_identity_groups,
    run_case_merge,
)

_monotonic = time.monotonic

# Closed set of values for ``RunSummary.stopped_because``.
STOP_DRY_RUN = "dry_run"
STOP_COMPLETED = "completed"
STOP_MAX_BATCHES = "max_batches"
STOP_STOP_FILE = "stop_file"
STOP_TIME_BUDGET = "time_budget"
STOP_ERROR = "error"
STOP_NO_PROGRESS = "no_progress"
STOP_INTEGRITY_FAILED = "integrity_failed"
STOP_REASONS = (
    STOP_DRY_RUN,
    STOP_COMPLETED,
    STOP_MAX_BATCHES,
    STOP_STOP_FILE,
    STOP_TIME_BUDGET,
    STOP_ERROR,
    STOP_NO_PROGRESS,
    STOP_INTEGRITY_FAILED,
)


class RunnerRefused(RuntimeError):
    """The run was refused before writing anything (missing backup reference,
    or the database is not quiescent)."""


@dataclass
class RunSummary:
    mode: str
    batch_size: int
    backup_ref: Optional[str] = None
    quiescence_checked: bool = False
    groups_before: int = 0
    batches_done: int = 0
    groups_merged: int = 0
    losers_deleted: int = 0
    rows_reowned: int = 0
    remaining_groups: int = 0
    stopped_because: str = ""
    errors: List[str] = field(default_factory=list)
    batches: List[Dict[str, Any]] = field(default_factory=list)
    integrity_checks: List[Dict[str, Any]] = field(default_factory=list)
    plan: Optional[Dict[str, Any]] = None
    started_at: str = ""
    ended_at: str = ""
    avg_seconds_per_group: Optional[float] = None
    estimated_seconds_remaining: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def check_integrity(db: Session) -> Dict[str, int]:
    """Orphan child rows per table: ONE aggregate LEFT JOIN query per table of
    ``CHILD_TABLES_WITH_CASE_ID`` (a fixed count, independent of data size)."""
    orphans: Dict[str, int] = {}
    for model in CHILD_TABLES_WITH_CASE_ID:
        orphans[model.__tablename__] = (
            db.query(func.count())
            .select_from(model)
            .outerjoin(Case, Case.id == model.case_id)
            .filter(model.case_id.isnot(None), Case.id.is_(None))
            .scalar()
            or 0
        )
    return orphans


def assert_quiescent(db: Session, quiescence_s: int) -> None:
    """Refuse (``RunnerRefused``) when the worker or the API wrote recently.

    Two aggregate queries: ``max(movements.created_at)`` and
    ``max(sync_history.started_at / completed_at)``. A timestamp in the future
    counts as recent. This is the guard behind "stop the worker".
    """
    cutoff = datetime.utcnow() - timedelta(seconds=quiescence_s)
    last_movement = db.query(func.max(Movement.created_at)).scalar()
    if last_movement is not None and last_movement > cutoff:
        raise RunnerRefused(
            f"movements.created_at is within the last {quiescence_s}s ({last_movement}): "
            "the worker (or the API) is still writing. Stop the worker and retry."
        )
    last_started, last_completed = db.query(
        func.max(SyncHistory.started_at), func.max(SyncHistory.completed_at)
    ).one()
    for column, value in (("started_at", last_started), ("completed_at", last_completed)):
        if value is not None and value > cutoff:
            raise RunnerRefused(
                f"sync_history.{column} is within the last {quiescence_s}s ({value}): "
                "a sync is running or just finished. Stop the worker and retry."
            )


def run_batches(
    session_factory: Callable[[], Session],
    *,
    batch_size: int,
    max_batches: Optional[int] = None,
    apply: bool = False,
    stop_file: Optional[str] = None,
    time_budget_s: Optional[float] = None,
    quiescence_s: int = 600,
    require_quiescent: bool = True,
    backup_ref: Optional[str] = None,
    verify_every: int = 10,
    log: Callable[[str], Any] = print,
) -> RunSummary:
    """Plan (default) or execute the duplicate-groups cleanup in batches.

    Raises ``RunnerRefused`` before touching anything when ``apply`` lacks a
    ``backup_ref`` or the database is not quiescent.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if verify_every < 1:
        raise ValueError("verify_every must be >= 1")

    summary = RunSummary(
        mode="apply" if apply else "dry-run",
        batch_size=batch_size,
        backup_ref=backup_ref.strip() if backup_ref else None,
        started_at=_now_iso(),
    )

    if not apply:
        return _plan(session_factory, summary)

    if not summary.backup_ref:
        raise RunnerRefused("--apply requires a backup reference (backup_ref) naming the verified backup.")
    if require_quiescent:
        with _session(session_factory) as db:
            assert_quiescent(db, quiescence_s)
        summary.quiescence_checked = True

    with _session(session_factory) as db:
        summary.groups_before = summary.remaining_groups = count_duplicate_identity_groups(db)

    started = _monotonic()
    last_checked_at = 0
    while True:
        if summary.remaining_groups == 0:
            summary.stopped_because = STOP_COMPLETED
        elif max_batches is not None and summary.batches_done >= max_batches:
            summary.stopped_because = STOP_MAX_BATCHES
        elif stop_file and Path(stop_file).exists():
            summary.stopped_because = STOP_STOP_FILE
        elif time_budget_s is not None and _monotonic() - started >= time_budget_s:
            summary.stopped_because = STOP_TIME_BUDGET
        if summary.stopped_because:
            break

        number = summary.batches_done + 1
        batch_started = _monotonic()
        db = session_factory()
        try:
            result = run_case_merge(db, group_limit=batch_size)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - any failure rolls back THIS batch and stops
            db.rollback()
            summary.errors.append(f"batch {number}: {type(exc).__name__}: {exc}")
            summary.stopped_because = STOP_ERROR
            log(f"lote {number}: ERROR, se revirtió solo este lote: {exc}")
            break
        finally:
            db.close()

        seconds = _monotonic() - batch_started
        summary.batches_done = number
        summary.groups_merged += result.merged_rol_count
        summary.losers_deleted += result.losers_deleted
        summary.rows_reowned += result.reowned_case_count
        summary.remaining_groups = result.remaining_groups
        summary.batches.append(
            {
                "batch": number,
                "groups_merged": result.merged_rol_count,
                "losers_deleted": result.losers_deleted,
                "rows_reowned": result.reowned_case_count,
                "remaining_groups": result.remaining_groups,
                "seconds": seconds,
                "group_keys": [list(key) for key in result.group_keys],
            }
        )
        log(
            f"lote {number}: grupos={result.merged_rol_count} perdedoras_borradas={result.losers_deleted} "
            f"filas_reasignadas={result.reowned_case_count} restantes={result.remaining_groups} "
            f"seg={seconds:.2f}"
        )

        if result.merged_rol_count == 0 and result.remaining_groups > 0:
            summary.errors.append(
                f"batch {number}: merged 0 groups with {result.remaining_groups} remaining; no progress"
            )
            summary.stopped_because = STOP_NO_PROGRESS
            break
        if number % verify_every == 0:
            last_checked_at = number
            if not _verify(session_factory, summary, number, log):
                break

    if summary.batches_done > last_checked_at and summary.stopped_because != STOP_INTEGRITY_FAILED:
        _verify(session_factory, summary, summary.batches_done, log)

    _finish(summary)
    return summary


def _plan(session_factory: Callable[[], Session], summary: RunSummary) -> RunSummary:
    with _session(session_factory) as db:
        groups = count_duplicate_identity_groups(db)
        first_batch = _select_batch_identities(db, summary.batch_size, None)
    summary.groups_before = summary.remaining_groups = groups
    summary.plan = {
        "groups": groups,
        "batch_size": summary.batch_size,
        "batches_needed": -(-groups // summary.batch_size),
        "first_batch_keys": [list(key) for key in first_batch],
    }
    summary.stopped_because = STOP_DRY_RUN
    _finish(summary)
    return summary


def _verify(
    session_factory: Callable[[], Session], summary: RunSummary, after_batch: int, log: Callable[[str], Any]
) -> bool:
    """Run the orphan check; on failure record it and mark the run stopped."""
    try:
        with _session(session_factory) as db:
            orphans = check_integrity(db)
    except Exception as exc:  # noqa: BLE001 - an unverifiable state is a failed check
        summary.integrity_checks.append({"after_batch": after_batch, "ok": False, "error": str(exc)})
        summary.errors.append(f"integrity check after batch {after_batch} could not run: {exc}")
        summary.stopped_because = STOP_INTEGRITY_FAILED
        log(f"INTEGRIDAD: no se pudo verificar tras el lote {after_batch}: {exc}")
        return False
    bad = {table: n for table, n in orphans.items() if n}
    summary.integrity_checks.append({"after_batch": after_batch, "ok": not bad, "orphans": orphans})
    if bad:
        detail = ", ".join(f"{table}={n}" for table, n in bad.items())
        summary.errors.append(f"integrity check after batch {after_batch}: orphan child rows: {detail}")
        summary.stopped_because = STOP_INTEGRITY_FAILED
        log(f"INTEGRIDAD FALLIDA tras el lote {after_batch}: filas huérfanas {detail}. DETENER e investigar.")
        return False
    log(f"integridad OK tras el lote {after_batch} (0 huérfanas en {len(orphans)} tablas)")
    return True


def _finish(summary: RunSummary) -> None:
    summary.ended_at = _now_iso()
    if summary.groups_merged:
        total = sum(batch["seconds"] for batch in summary.batches)
        summary.avg_seconds_per_group = total / summary.groups_merged
        summary.estimated_seconds_remaining = summary.avg_seconds_per_group * summary.remaining_groups


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _session:
    """Context manager over a factory session that always closes it."""

    def __init__(self, factory: Callable[[], Session]):
        self._factory = factory

    def __enter__(self) -> Session:
        self._db = self._factory()
        return self._db

    def __exit__(self, *exc) -> None:
        self._db.close()
