"""Case merge pipeline (ADR-008, Slice 2) — merges duplicate Case rows.

Pre-Approach-C ingest wrote one Case per (syncing lawyer, ROL). Approach C
(PR1a/PR1b) fixed ingest to upsert one Case per causa under the firm's
canonical ``lawyer_id`` going forward, but historical duplicates already
exist (the same causa under Carla's id AND under one or more per-lawyer ids).
This module merges those duplicates: exactly one Case row survives per causa
identity ``(rol, court_id, procedure)``, owned by the firm ``lawyer_id``, with
every child row re-pointed and deduped. A causa is NEVER identified by ROL
alone: the same ROL in two courts, or in two cuadernos of one court
(``procedure``), are different juicios and both survive.

``run_case_merge`` is a pure, DB-session based pipeline — it never commits.
The caller (the Alembic migration, or a test) controls the transaction
boundary, which is what makes the verify-before-delete gate meaningful: if
``_verify_invariants`` raises, the caller can roll back the ENTIRE
transaction (repoint + dedupe + audit rows included), not just skip the
delete step.

Idempotent: re-running after a successful merge finds zero duplicate
identities and zero non-firm-owned survivors, so it is a no-op.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Collection, Dict, List, Optional, Tuple

from sqlalchemy import func, tuple_
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
from app.models.cartera_snapshot import CarteraSnapshot
from app.models.document import Document
from app.models.generated_document import GeneratedDocument
from app.models.hito import Hito
from app.models.liberacion import LiberacionRequest
from app.models.movement import Movement

# Every table with a ``case_id`` FK to ``cases.id`` (case_merge_audit's
# ``winner_case_id`` is excluded: it points at the WINNER, not a child). Each
# must be re-pointed loser -> winner before the loser Case row is deleted and
# is checked by ``_verify_invariants``. A metadata-driven test pins this list
# against ``Base.metadata`` so a future table cannot be forgotten silently.
CHILD_TABLES_WITH_CASE_ID = [
    Movement,
    Document,
    CaseLitigante,
    CaseNotificacion,
    CaseEscrito,
    CaseExhorto,
    CaseDeadline,
    Alert,
    Hito,
    LiberacionRequest,
    GeneratedDocument,
    CarteraSnapshot,
    CaseLawyerSource,
]

# (rol, court_id, procedure) with NULL and '' treated as the same cuaderno.
Identity = Tuple[str, int, str]


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
    remaining_groups: int = 0
    group_keys: List[Identity] = field(default_factory=list)


def run_case_merge(
    db: Session,
    *,
    group_limit: Optional[int] = None,
    only_group_keys: Optional[Collection[Identity]] = None,
) -> CaseMergeResult:
    """Merge every duplicate-identity group of Case rows into one firm-owned Case.

    Steps (per spec's "Data migration invariants" requirement):
    1. Find duplicate groups (civil cases, >1 Case row per
       ``(rol, court_id, procedure)``; NULL and '' procedure are equal).
    2. Select the winner per group: richest (most movements+documents+
       litigantes), tie-broken by lowest ``id``.
    3. Backfill ``case_lawyer_source`` for every pre-merge ``lawyer_id`` in
       the group (so the merge doesn't lose the per-lawyer sync signal).
    4. Snapshot the loser->winner mapping into ``case_merge_audit``.
    5. Re-point all child tables (``CHILD_TABLES_WITH_CASE_ID``) loser_case_id -> winner_case_id.
    6. Dedupe children per natural key (true duplicates collapse; distinct
       rows from both sides are preserved).
    7. VERIFY invariants — raises ``CaseMergeVerificationError`` and deletes
       NOTHING if verification fails.
    8. Delete loser Case rows (only reached if verification passed).
    9. Re-own every surviving Case to the firm ``lawyer_id`` — deliberately
       AFTER delete, not before: a loser can legitimately already be
       firm-owned, and re-owning the winner while that loser still exists
       would transiently violate ``uq_cases_identity``.

    Does not commit. Idempotent: with no duplicate identities left, this is a
    cheap no-op (re-own is scoped to non-firm-owned cases only).

    Batching (for resumable cleanups, one transaction per call):
    ``only_group_keys`` restricts the run to those identities (unknown keys
    are ignored, so resuming with an already-merged key is a no-op) and
    ``group_limit`` then keeps the first N groups in ascending
    ``(court_id, rol, procedure)`` order. Merged groups stop being
    duplicates, so the next call over the same ordering returns the NEXT
    groups. When either argument is given, verification (step 7) and re-own
    (step 9) are scoped to the groups/winners of this batch; with neither,
    the behaviour is the full-table one. ``remaining_groups`` counts the
    groups still duplicated after the batch.
    """
    firm_id = firm_lawyer_id(db)
    batched = group_limit is not None or only_group_keys is not None
    identities = _select_batch_identities(db, group_limit, only_group_keys)
    groups = _load_groups(db, identities)

    losers_by_identity: Dict[Identity, List[Case]] = {}
    audit_rows_created = 0

    winner_ids: Dict[Identity, int] = {}

    for identity, cases in groups.items():
        rol = identity[0]
        winner = _select_winner(db, cases)
        winner_ids[identity] = winner.id
        losers = [c for c in cases if c.id != winner.id]
        losers_by_identity[identity] = losers

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
            _merge_children(db, loser.id, winner.id)
            db.flush()

    loser_ids = [loser.id for losers in losers_by_identity.values() for loser in losers]

    # Verify BEFORE any Case delete (spec invariants 1-3: no dup identities among
    # survivors, no orphaned children, natural-key dedupe integrity).
    if batched:
        _verify_invariants(db, loser_ids, identities=list(groups))
    else:
        _verify_invariants(db, loser_ids)

    losers_deleted = 0
    for losers in losers_by_identity.values():
        for loser in losers:
            db.delete(loser)
            losers_deleted += 1
    db.flush()

    # Re-own AFTER delete, not before: a loser can legitimately already be
    # firm-owned (e.g. the firm's own whole-firm scrape loses richness to a
    # lawyer's extension copy for the same ROL). Re-owning the winner to
    # firm_id while that not-yet-deleted loser still holds
    # (firm_id, rol, court, procedure) would transiently violate uq_cases_identity. Deleting
    # losers first makes re-own unconditionally safe; invariant 4 ("every
    # surviving Case is owned by the firm lawyer_id") then holds by
    # construction rather than needing a separate pre-delete check.
    # In a batch only this batch's winners are re-owned: the rest of the table
    # still holds groups that were not merged yet, and touching them would
    # both pre-empt their own batch and pay a table-wide UPDATE every call.
    reowned = _reown_surviving_cases(
        db,
        firm_id,
        exclude_ids=[],
        only_ids=[winner_ids[identity] for identity in groups] if batched else None,
    )
    db.flush()

    return CaseMergeResult(
        merged_rol_count=len(groups),
        losers_deleted=losers_deleted,
        audit_rows_created=audit_rows_created,
        reowned_case_count=reowned,
        merged_rols=sorted(identity[0] for identity in groups),
        remaining_groups=count_duplicate_identity_groups(db),
        group_keys=list(groups),
    )


def count_duplicate_identity_groups(db: Session) -> int:
    """Number of civil identities still held by more than one Case row, in ONE
    aggregate statement (same GROUP BY ... HAVING shape as
    ``_find_duplicate_identity_groups``, without loading any case)."""
    procedure_key = func.coalesce(Case.procedure, "")
    grouped = (
        db.query(Case.rol)
        .filter(Case.competencia == "civil")
        .group_by(Case.rol, Case.court_id, procedure_key)
        .having(func.count(Case.id) > 1)
        .subquery()
    )
    return db.query(func.count()).select_from(grouped).scalar() or 0


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

    Idempotent: skips an audit row if a Case with that (lawyer_id, rol,
    court_id, procedure) already exists — the winner's court and cuaderno
    identify the causa, so a different causa with the same ROL is not a skip.

    Returns the number of Case rows recreated.
    """
    recreated = 0
    audit_rows = db.query(CaseMergeAudit).all()
    for row in audit_rows:
        winner = db.query(Case).filter(Case.id == row.winner_case_id).first()
        if winner is None:
            continue

        existing = (
            db.query(Case)
            .filter(
                Case.lawyer_id == row.loser_lawyer_id,
                Case.rol == row.rol,
                Case.court_id == winner.court_id,
                func.coalesce(Case.procedure, "") == (winner.procedure or ""),
            )
            .first()
        )
        if existing is not None:
            continue

        db.add(
            Case(
                lawyer_id=row.loser_lawyer_id,
                court_id=winner.court_id,
                rol=row.rol,
                procedure=winner.procedure,
                competencia=winner.competencia,
            )
        )
        recreated += 1
    db.flush()
    return recreated


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_duplicate_identities(db: Session) -> List[Identity]:
    """Identities of civil causas with more than one Case row, sorted by
    ``(court_id, rol, procedure)``. NULL and '' procedure are the same
    cuaderno. The order depends only on the identities themselves, so it is
    stable across batches."""
    procedure_key = func.coalesce(Case.procedure, "")
    dupes = (
        db.query(Case.rol, Case.court_id, procedure_key)
        .filter(Case.competencia == "civil")
        .group_by(Case.rol, Case.court_id, procedure_key)
        .having(func.count(Case.id) > 1)
        .all()
    )
    return sorted(
        ((rol, court_id, procedure or "") for rol, court_id, procedure in dupes),
        key=lambda identity: (identity[1], identity[0], identity[2]),
    )


def _select_batch_identities(
    db: Session,
    group_limit: Optional[int],
    only_group_keys: Optional[Collection[Identity]],
) -> List[Identity]:
    """Duplicate identities for this run: filtered by ``only_group_keys``
    (keys that are not duplicated any more are ignored), then cut to the
    first ``group_limit`` in sorted order."""
    identities = _find_duplicate_identities(db)
    if only_group_keys is not None:
        wanted = {(rol, court_id, procedure or "") for rol, court_id, procedure in only_group_keys}
        identities = [identity for identity in identities if identity in wanted]
    if group_limit is not None:
        identities = identities[: max(group_limit, 0)]
    return identities


def _load_groups(db: Session, identities: List[Identity]) -> Dict[Identity, List[Case]]:
    """Cases of each identity, ordered by id."""
    procedure_key = func.coalesce(Case.procedure, "")
    groups: Dict[Identity, List[Case]] = {}
    for rol, court_id, procedure in identities:
        groups[(rol, court_id, procedure)] = (
            db.query(Case)
            .filter(
                Case.rol == rol,
                Case.court_id == court_id,
                procedure_key == procedure,
                Case.competencia == "civil",
            )
            .order_by(Case.id)
            .all()
        )
    return groups


def _find_duplicate_identity_groups(db: Session) -> Dict[Identity, List[Case]]:
    """Civil causas with more than one Case row per identity, each group
    ordered by id. Identity is ``(rol, court_id, procedure)``, never ROL
    alone; NULL and '' procedure are the same cuaderno."""
    return _load_groups(db, _find_duplicate_identities(db))


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


def _merge_children(db: Session, loser_id: int, winner_id: int) -> None:
    """Collision-aware loser -> winner merge for every child table.

    Replaces the old two-phase "blind repoint (UPDATE case_id=winner for
    every loser row) THEN dedupe" flow. That flow could emit an UPDATE that
    collides with a row the winner ALREADY has for the same natural
    identity (e.g. two CaseLitigante rows with the same ``natural_key``)
    before the dedupe step ever ran — fine on SQLite's default test session,
    but a hard ``UniqueViolation`` on Postgres for any table with a per-case
    UNIQUE constraint (real incident: ``uq_case_litigante_natural_key``).

    Each ``_merge_*`` helper below builds the winner's existing identity set
    FIRST, then for every loser row either:
    - the identity already exists on the winner -> repoint any references
      to the loser row onto the winner's canonical row, then DELETE the
      loser row (never UPDATE its case_id into a colliding identity), or
    - the identity is new -> UPDATE case_id to the winner (safe, no
      collision) and register it as the new canonical row.

    Order matters: Movement and CaseEscrito must be merged before Document
    (whose movement_id/escrito_id references must resolve to already-final
    canonical ids) and before CaseDeadline/Alert (source_movement_id /
    movement_id same reasoning).
    """
    _merge_movements(db, loser_id, winner_id)
    _merge_natural_key(db, CaseLitigante, loser_id, winner_id)
    _merge_natural_key(db, CaseNotificacion, loser_id, winner_id)
    _merge_escritos(db, loser_id, winner_id)
    _merge_natural_key(db, CaseExhorto, loser_id, winner_id)
    _merge_deadlines(db, loser_id, winner_id)
    _merge_documents(db, loser_id, winner_id)
    _merge_alerts(db, loser_id, winner_id)
    _merge_case_lawyer_sources(db, loser_id, winner_id)
    _merge_cartera_snapshots(db, loser_id, winner_id)
    # No per-case constraint: plain repoint.
    for model in (Hito, LiberacionRequest, GeneratedDocument):
        for row in db.query(model).filter(model.case_id == loser_id).all():
            row.case_id = winner_id


def _merge_movements(db: Session, loser_id: int, winner_id: int) -> None:
    """Movements have no DB unique constraint — natural identity is
    (folio, description), mirroring ``SyncService._upsert_movement``. Any
    Document/CaseDeadline/Alert referencing a colliding loser movement is
    re-pointed to the winner's canonical row before the loser row is
    deleted.

    The reference repoints are flushed BEFORE the loser rows are deleted:
    Document has an ORM relationship back to its parent (``escrito`` with
    ``backref="documents"``-style bidirectional tracking elsewhere in this
    module) and SQLAlchemy's unit-of-work will otherwise re-query and
    nullify still-referencing children as part of processing the DELETE,
    undoing an in-memory-only reassignment that was never flushed first.
    """
    seen: Dict[tuple, Movement] = {}
    for row in db.query(Movement).filter(Movement.case_id == winner_id).order_by(Movement.id).all():
        seen.setdefault((row.folio, row.description), row)

    to_delete: List[Movement] = []
    for row in db.query(Movement).filter(Movement.case_id == loser_id).order_by(Movement.id).all():
        key = (row.folio, row.description)
        canonical = seen.get(key)
        if canonical is not None:
            for doc in db.query(Document).filter(Document.movement_id == row.id).all():
                doc.movement_id = canonical.id
            for deadline in db.query(CaseDeadline).filter(CaseDeadline.source_movement_id == row.id).all():
                deadline.source_movement_id = canonical.id
            for deadline in db.query(CaseDeadline).filter(CaseDeadline.verdict_movement_id == row.id).all():
                deadline.verdict_movement_id = canonical.id
            for alert in db.query(Alert).filter(Alert.movement_id == row.id).all():
                alert.movement_id = canonical.id
            for hito in db.query(Hito).filter(Hito.movement_id == row.id).all():
                hito.movement_id = canonical.id
            to_delete.append(row)
        else:
            row.case_id = winner_id
            seen[key] = row

    if to_delete:
        db.flush()
        for row in to_delete:
            db.delete(row)


def _merge_natural_key(db: Session, model, loser_id: int, winner_id: int) -> None:
    """Collision-aware merge for child tables whose only identity is
    `natural_key` (per-case UNIQUE constraint) and that nothing else
    references by id."""
    seen = set()
    for row in db.query(model).filter(model.case_id == winner_id).order_by(model.id).all():
        seen.add(row.natural_key)

    for row in db.query(model).filter(model.case_id == loser_id).order_by(model.id).all():
        if row.natural_key in seen:
            db.delete(row)
        else:
            row.case_id = winner_id
            seen.add(row.natural_key)


def _merge_escritos(db: Session, loser_id: int, winner_id: int) -> None:
    """CaseEscrito's identity is `natural_key` (per-case UNIQUE constraint),
    but Document.escrito_id may reference a colliding loser row and must be
    re-pointed to the winner's canonical row before that row is deleted.

    The Document.escrito_id repoint is flushed BEFORE the CaseEscrito row is
    deleted: Document.escrito relationship has ``backref="documents"`` on
    CaseEscrito, so SQLAlchemy's unit-of-work re-queries and nullifies any
    Document still referencing the escrito's PK as part of processing the
    DELETE — which would silently undo an unflushed in-memory repoint.
    """
    seen: Dict[str, CaseEscrito] = {}
    for row in db.query(CaseEscrito).filter(CaseEscrito.case_id == winner_id).order_by(CaseEscrito.id).all():
        seen.setdefault(row.natural_key, row)

    to_delete: List[CaseEscrito] = []
    for row in db.query(CaseEscrito).filter(CaseEscrito.case_id == loser_id).order_by(CaseEscrito.id).all():
        canonical = seen.get(row.natural_key)
        if canonical is not None:
            for doc in db.query(Document).filter(Document.escrito_id == row.id).all():
                doc.escrito_id = canonical.id
            to_delete.append(row)
        else:
            row.case_id = winner_id
            seen[row.natural_key] = row

    if to_delete:
        db.flush()
        for row in to_delete:
            db.delete(row)


def _merge_deadlines(db: Session, loser_id: int, winner_id: int) -> None:
    """CaseDeadline's identity is (deadline_type, triggered_at) per
    ``uq_case_deadline_type_triggered``, not a single natural_key column."""
    seen = set()
    for row in db.query(CaseDeadline).filter(CaseDeadline.case_id == winner_id).order_by(CaseDeadline.id).all():
        seen.add((row.deadline_type, row.triggered_at))

    for row in db.query(CaseDeadline).filter(CaseDeadline.case_id == loser_id).order_by(CaseDeadline.id).all():
        key = (row.deadline_type, row.triggered_at)
        if key in seen:
            db.delete(row)
        else:
            row.case_id = winner_id
            seen.add(key)


def _merge_documents(db: Session, loser_id: int, winner_id: int) -> None:
    """Documents with a ``pjud_token_hash`` are already globally unique on
    that hash (partial unique index, migration 006) — a hash collision
    across two duplicate-ROL cases could never have been persisted in the
    first place, so they are always safely repointed, never deduped. Only
    hash-less (extension-uploaded, no stable identity) documents can
    actually collide post-repoint, on (movement_id, escrito_id) — but ONLY
    when at least one of the two is set. A document with neither link (a
    standalone, hash-less upload) has no real natural-key basis for
    comparison: two such rows sharing the degenerate ``(None, None)`` key
    are NOT necessarily the same document, so they must never be silently
    collapsed (that would be data loss)."""
    seen = set()
    for row in (
        db.query(Document)
        .filter(Document.case_id == winner_id, Document.pjud_token_hash.is_(None))
        .order_by(Document.id)
        .all()
    ):
        if row.movement_id is not None or row.escrito_id is not None:
            seen.add((row.movement_id, row.escrito_id))

    for row in db.query(Document).filter(Document.case_id == loser_id).order_by(Document.id).all():
        if row.pjud_token_hash is None and (row.movement_id is not None or row.escrito_id is not None):
            key = (row.movement_id, row.escrito_id)
            if key in seen:
                db.delete(row)
                continue
            seen.add(key)
        row.case_id = winner_id


def _merge_alerts(db: Session, loser_id: int, winner_id: int) -> None:
    """Alerts have no natural key or DB unique constraint; per design
    ADR-008 decision, collapse exact duplicates on
    (lawyer_id, movement_id, type, message)."""
    seen = set()
    for row in db.query(Alert).filter(Alert.case_id == winner_id).order_by(Alert.id).all():
        seen.add((row.lawyer_id, row.movement_id, row.type, row.message))

    for row in db.query(Alert).filter(Alert.case_id == loser_id).order_by(Alert.id).all():
        key = (row.lawyer_id, row.movement_id, row.type, row.message)
        if key in seen:
            db.delete(row)
        else:
            row.case_id = winner_id
            seen.add(key)


def _merge_case_lawyer_sources(db: Session, loser_id: int, winner_id: int) -> None:
    """Unique per (case_id, lawyer_id): the loser's own source rows still
    reference the loser and would block its delete. ``_backfill_case_lawyer_
    source`` already gave the winner a row per pre-merge lawyer, so colliding
    loser rows are dropped and the rest repointed."""
    seen = {
        row.lawyer_id
        for row in db.query(CaseLawyerSource).filter(CaseLawyerSource.case_id == winner_id).all()
    }
    for row in db.query(CaseLawyerSource).filter(CaseLawyerSource.case_id == loser_id).order_by(CaseLawyerSource.id).all():
        if row.lawyer_id in seen:
            db.delete(row)
        else:
            row.case_id = winner_id
            seen.add(row.lawyer_id)


def _merge_cartera_snapshots(db: Session, loser_id: int, winner_id: int) -> None:
    """Unique per (periodo, case_id): the winner keeps its snapshot for a
    period both copies froze; periods only the loser has are repointed."""
    seen = {
        row.periodo
        for row in db.query(CarteraSnapshot).filter(CarteraSnapshot.case_id == winner_id).all()
    }
    for row in db.query(CarteraSnapshot).filter(CarteraSnapshot.case_id == loser_id).order_by(CarteraSnapshot.id).all():
        if row.periodo in seen:
            db.delete(row)
        else:
            row.case_id = winner_id
            seen.add(row.periodo)


def _reown_surviving_cases(
    db: Session,
    firm_id: int,
    exclude_ids: List[int],
    only_ids: Optional[List[int]] = None,
) -> int:
    """Set lawyer_id = firm_id for every civil Case not about to be deleted.

    Scoped to non-firm-owned rows so a no-duplicates re-run reports 0
    (idempotency signal), not just "no functional change". ``only_ids``
    (a batch's winners) restricts it further; an empty list re-owns nothing.
    """
    if only_ids is not None and not only_ids:
        return 0
    query = db.query(Case).filter(Case.lawyer_id != firm_id, ~Case.id.in_(exclude_ids or [0]))
    if only_ids is not None:
        query = query.filter(Case.id.in_(only_ids))
    rows = query.all()
    for row in rows:
        row.lawyer_id = firm_id
    return len(rows)


def _verify_invariants(
    db: Session,
    loser_ids: List[int],
    identities: Optional[Collection[Identity]] = None,
) -> None:
    """Verify merge invariants BEFORE any loser Case row is deleted.

    Raises ``CaseMergeVerificationError`` (nothing deleted) if any of:
    1. Zero duplicate identities (rol, court_id, procedure) remain among
       survivors (post-repoint/dedupe).
    2. Zero child rows still reference a to-be-deleted loser case_id
       (would become orphaned the instant the loser Case is deleted).

    Invariant 4 ("every surviving Case is owned by the firm lawyer_id") is
    NOT checked here: re-owning happens AFTER delete (see ``run_case_merge``
    docstring for why — checking/enforcing it pre-delete would itself
    transiently violate ``uq_cases_identity`` whenever a not-yet-deleted
    loser already happens to be firm-owned). It is guaranteed by
    construction once ``_reown_surviving_cases`` runs post-delete.

    ``identities`` scopes check 1 to the groups of a batch (groups outside
    it legitimately still hold duplicates); ``None`` checks the whole table.
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

    procedure_key = func.coalesce(Case.procedure, "")
    dupe_query = (
        db.query(Case.rol, Case.court_id, procedure_key)
        .filter(Case.competencia == "civil", ~Case.id.in_(loser_ids or [0]))
        .group_by(Case.rol, Case.court_id, procedure_key)
        .having(func.count(Case.id) > 1)
    )
    if identities is not None:
        if not identities:
            dupe_query = dupe_query.filter(False)
        else:
            dupe_query = dupe_query.filter(
                tuple_(Case.rol, Case.court_id, procedure_key).in_(
                    [(rol, court_id, procedure or "") for rol, court_id, procedure in identities]
                )
            )
    dupe_identities = dupe_query.all()
    if dupe_identities:
        failures.append(
            "duplicate identities remain among survivors: "
            + ", ".join(
                f"{rol} (court {court_id}, procedure {procedure!r})"
                for rol, court_id, procedure in sorted(dupe_identities, key=str)
            )
        )

    if failures:
        raise CaseMergeVerificationError(
            "Case merge verification failed; ABORTING before any Case row is deleted:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )
