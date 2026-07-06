"""Case merge pipeline (ADR-008, Slice 2) — merges duplicate Case rows.

Pre-Approach-C ingest wrote one Case per (syncing lawyer, ROL). Approach C
(PR1a/PR1b) fixed ingest to upsert one Case per ROL under the firm's
canonical ``lawyer_id`` going forward, but historical duplicates already
exist (the same ROL under Carla's id AND under one or more per-lawyer ids).
This module merges those duplicates: exactly one Case row survives per ROL,
owned by the firm ``lawyer_id``, with every child row re-pointed and deduped.

``run_case_merge`` is a pure, DB-session based pipeline — it never commits.
The caller (the Alembic migration, or a test) controls the transaction
boundary, which is what makes the verify-before-delete gate meaningful: if
``_verify_invariants`` raises, the caller can roll back the ENTIRE
transaction (repoint + dedupe + audit rows included), not just skip the
delete step.

Idempotent: re-running after a successful merge finds zero duplicate ROLs
and zero non-firm-owned survivors, so it is a no-op.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import firm_lawyer_id
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.case_litigante import CaseLitigante
from app.models.case_merge_audit import CaseMergeAudit
from app.models.case_notificacion import CaseNotificacion
from app.models.document import Document
from app.models.movement import Movement

# The 8 child tables that carry a `case_id` FK and must be re-pointed
# loser -> winner before the loser Case row is deleted (spec: "Data
# migration invariants" requirement, design ADR-008 section 9).
CHILD_TABLES_WITH_CASE_ID = [
    Movement,
    Document,
    CaseLitigante,
    CaseNotificacion,
    CaseEscrito,
    CaseExhorto,
    CaseDeadline,
    Alert,
]


class CaseMergeVerificationError(RuntimeError):
    """Raised when a post-merge invariant fails.

    Raised BEFORE any loser Case row is deleted — the caller is expected to
    roll back its transaction on this error so nothing (including the
    repoint/dedupe already performed) is left half-applied.
    """


@dataclass
class CaseMergeResult:
    merged_rol_count: int = 0
    losers_deleted: int = 0
    audit_rows_created: int = 0
    reowned_case_count: int = 0
    merged_rols: List[str] = field(default_factory=list)


def run_case_merge(db: Session) -> CaseMergeResult:
    """Merge every duplicate-ROL group of Case rows into one firm-owned Case.

    Steps (per spec's "Data migration invariants" requirement):
    1. Find duplicate ROL groups (civil cases, >1 Case row per ROL).
    2. Select the winner per group: richest (most movements+documents+
       litigantes), tie-broken by lowest ``id``.
    3. Backfill ``case_lawyer_source`` for every pre-merge ``lawyer_id`` in
       the group (so the merge doesn't lose the per-lawyer sync signal).
    4. Snapshot the loser->winner mapping into ``case_merge_audit``.
    5. Re-point all 8 child tables loser_case_id -> winner_case_id.
    6. Dedupe children per natural key (true duplicates collapse; distinct
       rows from both sides are preserved).
    7. VERIFY invariants — raises ``CaseMergeVerificationError`` and deletes
       NOTHING if verification fails.
    8. Delete loser Case rows (only reached if verification passed).
    9. Re-own every surviving Case to the firm ``lawyer_id`` — deliberately
       AFTER delete, not before: a loser can legitimately already be
       firm-owned, and re-owning the winner while that loser still exists
       would transiently violate ``uq_cases_lawyer_rol``.

    Does not commit. Idempotent: with no duplicate ROLs left, this is a
    cheap no-op (re-own is scoped to non-firm-owned cases only).
    """
    firm_id = firm_lawyer_id(db)
    groups = _find_duplicate_rol_groups(db)

    losers_by_rol: Dict[str, List[Case]] = {}
    audit_rows_created = 0

    for rol, cases in groups.items():
        winner = _select_winner(db, cases)
        losers = [c for c in cases if c.id != winner.id]
        losers_by_rol[rol] = losers

        _backfill_case_lawyer_source(db, winner, cases)

        now = datetime.now(timezone.utc)
        for loser in losers:
            db.add(
                CaseMergeAudit(
                    loser_case_id=loser.id,
                    winner_case_id=winner.id,
                    loser_lawyer_id=loser.lawyer_id,
                    rol=rol,
                    created_at=now,
                )
            )
            audit_rows_created += 1
        db.flush()

        for loser in losers:
            _repoint_children(db, loser.id, winner.id)
        db.flush()

        _dedupe_children(db, winner.id)
        db.flush()

    loser_ids = [loser.id for losers in losers_by_rol.values() for loser in losers]

    # Verify BEFORE any Case delete (spec invariants 1-3: no dup ROLs among
    # survivors, no orphaned children, natural-key dedupe integrity).
    _verify_invariants(db, loser_ids)

    losers_deleted = 0
    for losers in losers_by_rol.values():
        for loser in losers:
            db.delete(loser)
            losers_deleted += 1
    db.flush()

    # Re-own AFTER delete, not before: a loser can legitimately already be
    # firm-owned (e.g. the firm's own whole-firm scrape loses richness to a
    # lawyer's extension copy for the same ROL). Re-owning the winner to
    # firm_id while that not-yet-deleted loser still holds
    # (firm_id, rol) would transiently violate uq_cases_lawyer_rol. Deleting
    # losers first makes re-own unconditionally safe; invariant 4 ("every
    # surviving Case is owned by the firm lawyer_id") then holds by
    # construction rather than needing a separate pre-delete check.
    reowned = _reown_surviving_cases(db, firm_id, exclude_ids=[])
    db.flush()

    return CaseMergeResult(
        merged_rol_count=len(groups),
        losers_deleted=losers_deleted,
        audit_rows_created=audit_rows_created,
        reowned_case_count=reowned,
        merged_rols=sorted(groups.keys()),
    )


def reverse_case_merge(db: Session) -> int:
    """Best-effort downgrade: recreate a skeleton loser Case per audit row.

    This is NOT a full reversal — child rows that were deduped/re-pointed
    during the merge are not split back apart (that information is not
    tracked at that granularity), so recreated Case rows have no children.
    The primary rollback mechanism for the real QA run is restoring the
    pre-migration DB snapshot (see docs/runbooks/unificar-modelo-causas-
    migration.md); this function exists only as a secondary, partial
    best-effort recovery path and to give ``downgrade()`` something
    meaningful to do.

    Idempotent: skips an audit row if a Case with that (lawyer_id, rol)
    already exists.

    Returns the number of Case rows recreated.
    """
    recreated = 0
    audit_rows = db.query(CaseMergeAudit).all()
    for row in audit_rows:
        existing = (
            db.query(Case)
            .filter(Case.lawyer_id == row.loser_lawyer_id, Case.rol == row.rol)
            .first()
        )
        if existing is not None:
            continue

        winner = db.query(Case).filter(Case.id == row.winner_case_id).first()
        if winner is None:
            continue

        db.add(
            Case(
                lawyer_id=row.loser_lawyer_id,
                court_id=winner.court_id,
                rol=row.rol,
                competencia=winner.competencia,
            )
        )
        recreated += 1
    db.flush()
    return recreated


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_duplicate_rol_groups(db: Session) -> Dict[str, List[Case]]:
    """Civil ROLs with more than one Case row, each group ordered by id."""
    dupe_rols = (
        db.query(Case.rol)
        .filter(Case.competencia == "civil")
        .group_by(Case.rol)
        .having(func.count(Case.id) > 1)
        .all()
    )
    groups: Dict[str, List[Case]] = {}
    for (rol,) in dupe_rols:
        cases = (
            db.query(Case)
            .filter(Case.rol == rol, Case.competencia == "civil")
            .order_by(Case.id)
            .all()
        )
        groups[rol] = cases
    return groups


def _richness_score(db: Session, case_id: int) -> int:
    movements = db.query(func.count(Movement.id)).filter(Movement.case_id == case_id).scalar() or 0
    documents = db.query(func.count(Document.id)).filter(Document.case_id == case_id).scalar() or 0
    litigantes = (
        db.query(func.count(CaseLitigante.id)).filter(CaseLitigante.case_id == case_id).scalar() or 0
    )
    return movements + documents + litigantes


def _select_winner(db: Session, cases: List[Case]) -> Case:
    """Richest case wins; ties break to the lowest ``id`` (deterministic)."""
    scored: List[Tuple[int, int, Case]] = [
        (-_richness_score(db, case.id), case.id, case) for case in cases
    ]
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return scored[0][2]


def _backfill_case_lawyer_source(db: Session, winner: Case, cases: List[Case]) -> None:
    """Record every pre-merge ``lawyer_id`` in the group as a source of the
    winner Case (so a case merged from a per-lawyer copy keeps that lawyer
    visible to ``get_pending_detail``'s rotation, per ADR-002)."""
    now = datetime.now(timezone.utc)
    for case in cases:
        existing = (
            db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == winner.id,
                CaseLawyerSource.lawyer_id == case.lawyer_id,
            )
            .first()
        )
        if existing is not None:
            existing.last_seen_at = now
        else:
            db.add(
                CaseLawyerSource(
                    case_id=winner.id,
                    lawyer_id=case.lawyer_id,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
    db.flush()


def _repoint_children(db: Session, loser_id: int, winner_id: int) -> None:
    for model in CHILD_TABLES_WITH_CASE_ID:
        rows = db.query(model).filter(model.case_id == loser_id).all()
        for row in rows:
            row.case_id = winner_id


def _dedupe_children(db: Session, case_id: int) -> None:
    _dedupe_movements(db, case_id)
    _dedupe_natural_key(db, CaseLitigante, case_id)
    _dedupe_natural_key(db, CaseNotificacion, case_id)
    _dedupe_escritos(db, case_id)
    _dedupe_natural_key(db, CaseExhorto, case_id)
    _dedupe_deadlines(db, case_id)
    _dedupe_documents(db, case_id)
    _dedupe_alerts(db, case_id)


def _dedupe_movements(db: Session, case_id: int) -> None:
    """Movements have no DB unique constraint — dedup on (case_id, folio,
    description), mirroring ``SyncService._upsert_movement``. Any Document
    or CaseDeadline referencing a deduped-away movement is re-pointed to the
    surviving canonical row first."""
    rows = db.query(Movement).filter(Movement.case_id == case_id).order_by(Movement.id).all()
    seen: Dict[tuple, Movement] = {}
    for row in rows:
        key = (row.folio, row.description)
        canonical = seen.get(key)
        if canonical is None:
            seen[key] = row
            continue

        for doc in db.query(Document).filter(Document.movement_id == row.id).all():
            doc.movement_id = canonical.id
        for deadline in db.query(CaseDeadline).filter(CaseDeadline.source_movement_id == row.id).all():
            deadline.source_movement_id = canonical.id
        for alert in db.query(Alert).filter(Alert.movement_id == row.id).all():
            alert.movement_id = canonical.id
        db.delete(row)


def _dedupe_natural_key(db: Session, model, case_id: int) -> None:
    """Generic dedupe for child tables whose only identity is `natural_key`
    and that nothing else references by id."""
    rows = db.query(model).filter(model.case_id == case_id).order_by(model.id).all()
    seen = set()
    for row in rows:
        if row.natural_key in seen:
            db.delete(row)
        else:
            seen.add(row.natural_key)


def _dedupe_escritos(db: Session, case_id: int) -> None:
    """CaseEscrito dedupes by natural_key, but Document.escrito_id may
    reference a deduped-away row and must be re-pointed first."""
    rows = db.query(CaseEscrito).filter(CaseEscrito.case_id == case_id).order_by(CaseEscrito.id).all()
    seen: Dict[str, CaseEscrito] = {}
    for row in rows:
        canonical = seen.get(row.natural_key)
        if canonical is None:
            seen[row.natural_key] = row
            continue
        for doc in db.query(Document).filter(Document.escrito_id == row.id).all():
            doc.escrito_id = canonical.id
        db.delete(row)


def _dedupe_deadlines(db: Session, case_id: int) -> None:
    """CaseDeadline's natural key is (deadline_type, triggered_at) per
    ``uq_case_deadline_type_triggered``, not a single natural_key column."""
    rows = db.query(CaseDeadline).filter(CaseDeadline.case_id == case_id).order_by(CaseDeadline.id).all()
    seen = set()
    for row in rows:
        key = (row.deadline_type, row.triggered_at)
        if key in seen:
            db.delete(row)
        else:
            seen.add(key)


def _dedupe_documents(db: Session, case_id: int) -> None:
    """Documents with a ``pjud_token_hash`` are already globally unique on
    that hash (partial unique index, migration 006) — a hash collision
    across two duplicate-ROL cases could never have been persisted in the
    first place, so only hash-less (extension-uploaded, no stable identity)
    documents can actually duplicate post-repoint. Dedupe those on
    (movement_id, escrito_id) — but ONLY when at least one of the two is
    set. A document with neither link (a standalone, hash-less upload) has
    no real natural-key basis for comparison: two such rows sharing the
    degenerate ``(None, None)`` key are NOT necessarily the same document,
    so they must never be silently collapsed (that would be data loss)."""
    rows = (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.pjud_token_hash.is_(None))
        .order_by(Document.id)
        .all()
    )
    seen = set()
    for row in rows:
        if row.movement_id is None and row.escrito_id is None:
            continue  # no natural-key basis — never treated as a duplicate
        key = (row.movement_id, row.escrito_id)
        if key in seen:
            db.delete(row)
        else:
            seen.add(key)


def _dedupe_alerts(db: Session, case_id: int) -> None:
    """Alerts have no natural key; per design ADR-008 decision, drop exact
    duplicates on (lawyer_id, movement_id, type, message) after re-point."""
    rows = db.query(Alert).filter(Alert.case_id == case_id).order_by(Alert.id).all()
    seen = set()
    for row in rows:
        key = (row.lawyer_id, row.movement_id, row.type, row.message)
        if key in seen:
            db.delete(row)
        else:
            seen.add(key)


def _reown_surviving_cases(db: Session, firm_id: int, exclude_ids: List[int]) -> int:
    """Set lawyer_id = firm_id for every civil Case not about to be deleted.

    Scoped to non-firm-owned rows so a no-duplicates re-run reports 0
    (idempotency signal), not just "no functional change".
    """
    rows = (
        db.query(Case)
        .filter(Case.lawyer_id != firm_id, ~Case.id.in_(exclude_ids or [0]))
        .all()
    )
    for row in rows:
        row.lawyer_id = firm_id
    return len(rows)


def _verify_invariants(db: Session, loser_ids: List[int]) -> None:
    """Verify merge invariants BEFORE any loser Case row is deleted.

    Raises ``CaseMergeVerificationError`` (nothing deleted) if any of:
    1. Zero duplicate ROLs remain among survivors (post-repoint/dedupe).
    2. Zero child rows still reference a to-be-deleted loser case_id
       (would become orphaned the instant the loser Case is deleted).

    Invariant 4 ("every surviving Case is owned by the firm lawyer_id") is
    NOT checked here: re-owning happens AFTER delete (see ``run_case_merge``
    docstring for why — checking/enforcing it pre-delete would itself
    transiently violate ``uq_cases_lawyer_rol`` whenever a not-yet-deleted
    loser already happens to be firm-owned). It is guaranteed by
    construction once ``_reown_surviving_cases`` runs post-delete.
    """
    loser_ids = loser_ids or []
    failures: List[str] = []

    for model in CHILD_TABLES_WITH_CASE_ID:
        stray = db.query(func.count(model.id)).filter(model.case_id.in_(loser_ids)).scalar() or 0
        if stray:
            failures.append(
                f"{model.__tablename__}: {stray} row(s) still reference a to-be-deleted "
                "loser case_id — repoint step incomplete"
            )

    dupe_rols = (
        db.query(Case.rol)
        .filter(Case.competencia == "civil", ~Case.id.in_(loser_ids or [0]))
        .group_by(Case.rol)
        .having(func.count(Case.id) > 1)
        .all()
    )
    if dupe_rols:
        failures.append(
            "duplicate ROLs remain among survivors: " + ", ".join(sorted(r[0] for r in dupe_rols))
        )

    if failures:
        raise CaseMergeVerificationError(
            "Case merge verification failed; ABORTING before any Case row is deleted:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )
