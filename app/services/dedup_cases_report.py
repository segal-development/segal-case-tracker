"""Read-only report of duplicate ``cases`` rows (T5a, duplicados-por-abogado).

Answers "what would the cleanup touch?" before anyone runs it. It NEVER calls
``run_case_merge``, never writes and never flushes pending ORM state: all
queries run inside ``db.no_autoflush`` and the caller is expected to roll the
session back when done.

Cost is FIXED, independent of the number of duplicate groups: one window-
function query finds the duplicate cases, one GROUP BY query per table in
``CHILD_TABLES_WITH_CASE_ID`` counts child rows per case, one query lists the
movement identities and one counts hitos per estado. Per-group / per-case
queries would take hours over the Cloud SQL proxy (~135 ms per round trip).

Group identity and winner rule MIRROR ``case_merge``: groups are civil cases
sharing ``(rol, court_id, coalesce(procedure, ''))``; the winner is the case
with most movements + documents + litigantes, ties to the lowest id (same as
``case_merge._select_winner``; a test pins that both agree).
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import firm_lawyer_id
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.case_litigante import CaseLitigante
from app.models.document import Document
from app.models.hito import Hito
from app.models.movement import Movement
from app.services.case_merge import CHILD_TABLES_WITH_CASE_ID

# Documented constant: ORM queries ``run_case_merge`` issues per loser
# (``_merge_children`` + audit + verify + delete), measured by reading the code.
QUERIES_PER_LOSER_ESTIMATE = 40
# Observed Cloud SQL proxy round trip from a residential IP.
ROUND_TRIP_SECONDS = 0.135

CASE_LAWYER_SOURCE_TABLE = CaseLawyerSource.__tablename__

MovementKey = Tuple[object, object]


def child_tables() -> list:
    """Every table with a ``case_id`` FK to ``cases``; the single source of truth is
    ``case_merge.CHILD_TABLES_WITH_CASE_ID`` so a new table is covered automatically."""
    return list(CHILD_TABLES_WITH_CASE_ID)


@dataclass
class GroupReport:
    rol: str
    court_id: int
    procedure: str
    copies: int
    winner_id: int
    winner_lawyer_id: int
    winner_changes_owner: bool
    loser_ids: List[int]
    empty_loser_ids: List[int]
    source_only_loser_ids: List[int]
    non_empty_loser_ids: List[int]
    movements_contained: int
    movements_unioned: int
    loser_rows_by_table: Dict[str, int]

    @property
    def is_diverging(self) -> bool:
        return self.movements_unioned > 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["is_diverging"] = self.is_diverging
        return data


@dataclass
class DedupReport:
    total_groups: int = 0
    surplus_rows: int = 0
    groups_by_copies: Dict[int, int] = field(default_factory=dict)
    surplus_rows_by_copies: Dict[int, int] = field(default_factory=dict)

    # Loser tiers. EMPTY: zero rows in every child table. SOURCE_ONLY: only
    # case_lawyer_source rows (the T3a backfill gives every case one for its own
    # lawyer), so the sighting must be carried to the winner before deleting.
    empty_losers: int = 0
    source_only_losers: int = 0
    non_empty_losers: int = 0
    empty_only_first_pass_rows: int = 0

    loser_rows_by_table: Dict[str, int] = field(default_factory=dict)
    hitos_on_losers_by_estado: Dict[str, int] = field(default_factory=dict)

    movements_contained_total: int = 0
    movements_unioned_total: int = 0
    diverging_groups: int = 0

    groups_spanning_court_or_competencia: int = 0
    groups_with_three_or_more_copies: int = 0
    groups_winner_changes_owner: int = 0

    estimate: dict = field(default_factory=dict)
    groups: List[GroupReport] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "totals": {
                "groups": self.total_groups,
                "surplus_rows": self.surplus_rows,
                "groups_by_copies": {str(k): v for k, v in sorted(self.groups_by_copies.items())},
                "surplus_rows_by_copies": {str(k): v for k, v in sorted(self.surplus_rows_by_copies.items())},
            },
            "loser_classification": {
                "empty": self.empty_losers,
                "source_only": self.source_only_losers,
                "non_empty": self.non_empty_losers,
                "empty_only_first_pass_rows": self.empty_only_first_pass_rows,
            },
            "loser_rows_by_table": dict(self.loser_rows_by_table),
            "hitos_on_losers_by_estado": dict(self.hitos_on_losers_by_estado),
            "movements": {
                "contained_in_winner": self.movements_contained_total,
                "would_be_unioned": self.movements_unioned_total,
                "diverging_groups": self.diverging_groups,
            },
            "risk": {
                "groups_spanning_court_or_competencia": self.groups_spanning_court_or_competencia,
                "groups_with_three_or_more_copies": self.groups_with_three_or_more_copies,
                "groups_winner_changes_owner": self.groups_winner_changes_owner,
            },
            "estimate": dict(self.estimate),
            "groups": [g.to_dict() for g in self.groups],
        }


def _estimate(losers: int, remaining_after_empty_pass: int) -> dict:
    def hours(n: int) -> float:
        return n * QUERIES_PER_LOSER_ESTIMATE * ROUND_TRIP_SECONDS / 3600

    return {
        "queries_per_loser": QUERIES_PER_LOSER_ESTIMATE,
        "round_trip_seconds": ROUND_TRIP_SECONDS,
        "formula": (
            f"hours = losers * {QUERIES_PER_LOSER_ESTIMATE} queries * {ROUND_TRIP_SECONDS} s / 3600"
        ),
        "losers": losers,
        "hours_all_losers": hours(losers),
        "losers_after_empty_pass": remaining_after_empty_pass,
        "hours_after_empty_pass": hours(remaining_after_empty_pass),
    }


def build_dedup_report(db: Session) -> DedupReport:
    """Build the full report with a fixed number of SELECTs. Never writes."""
    with db.no_autoflush:
        return _build(db)


def _build(db: Session) -> DedupReport:
    firm_id = firm_lawyer_id(db)
    tables = child_tables()

    procedure_key = func.coalesce(Case.procedure, "")
    copies = func.count(Case.id).over(partition_by=[Case.rol, Case.court_id, procedure_key])
    civil = (
        db.query(
            Case.id.label("id"),
            Case.lawyer_id.label("lawyer_id"),
            Case.rol.label("rol"),
            Case.court_id.label("court_id"),
            procedure_key.label("procedure"),
            Case.competencia.label("competencia"),
            copies.label("copies"),
        )
        .filter(Case.competencia == "civil")
        .subquery()
    )
    duplicate_rows = (
        db.query(civil).filter(civil.c.copies > 1).order_by(civil.c.rol, civil.c.court_id, civil.c.id).all()
    )
    duplicate_ids = select(civil.c.id).where(civil.c.copies > 1)

    by_identity: Dict[tuple, list] = defaultdict(list)
    for row in duplicate_rows:
        by_identity[(row.rol, row.court_id, row.procedure or "")].append(row)

    # table name -> case_id -> row count (only duplicate cases are queried).
    counts: Dict[str, Dict[int, int]] = {}
    for model in tables:
        counts[model.__tablename__] = dict(
            db.query(model.case_id, func.count(model.id))
            .filter(model.case_id.in_(duplicate_ids))
            .group_by(model.case_id)
            .all()
        )

    movement_keys: Dict[int, List[MovementKey]] = defaultdict(list)
    for case_id, folio, description in (
        db.query(Movement.case_id, Movement.folio, Movement.description)
        .filter(Movement.case_id.in_(duplicate_ids))
        .order_by(Movement.case_id, Movement.id)
        .all()
    ):
        movement_keys[case_id].append((folio, description))

    hitos_by_case_estado: Dict[int, Dict[str, int]] = defaultdict(dict)
    for case_id, estado, n in (
        db.query(Hito.case_id, Hito.estado, func.count(Hito.id))
        .filter(Hito.case_id.in_(duplicate_ids))
        .group_by(Hito.case_id, Hito.estado)
        .all()
    ):
        hitos_by_case_estado[case_id][estado] = n

    def richness(case_id: int) -> int:
        return sum(
            counts[model.__tablename__].get(case_id, 0) for model in (Movement, Document, CaseLitigante)
        )

    report = DedupReport(loser_rows_by_table={m.__tablename__: 0 for m in tables})

    for (rol, court_id, procedure), rows in by_identity.items():
        # Same rule as case_merge._select_winner: richest, ties to the lowest id.
        winner = min(rows, key=lambda r: (-richness(r.id), r.id))
        losers = sorted((r for r in rows if r.id != winner.id), key=lambda r: r.id)

        empty_ids: List[int] = []
        source_only_ids: List[int] = []
        non_empty_ids: List[int] = []
        group_rows_by_table = {m.__tablename__: 0 for m in tables}
        for loser in losers:
            per_table = {name: counts[name].get(loser.id, 0) for name in group_rows_by_table}
            for name, n in per_table.items():
                group_rows_by_table[name] += n
            others = sum(n for name, n in per_table.items() if name != CASE_LAWYER_SOURCE_TABLE)
            if others == 0 and per_table.get(CASE_LAWYER_SOURCE_TABLE, 0) == 0:
                empty_ids.append(loser.id)
            elif others == 0:
                source_only_ids.append(loser.id)
            else:
                non_empty_ids.append(loser.id)
            for estado, n in hitos_by_case_estado.get(loser.id, {}).items():
                report.hitos_on_losers_by_estado[estado] = report.hitos_on_losers_by_estado.get(estado, 0) + n

        # Merge identity (folio, description): losers are processed in id order and
        # each newly unioned movement becomes part of the winner for later losers.
        seen = set(movement_keys.get(winner.id, []))
        contained = unioned = 0
        for loser in losers:
            for key in movement_keys.get(loser.id, []):
                if key in seen:
                    contained += 1
                else:
                    unioned += 1
                    seen.add(key)

        n_copies = len(rows)
        report.total_groups += 1
        report.surplus_rows += len(losers)
        report.groups_by_copies[n_copies] = report.groups_by_copies.get(n_copies, 0) + 1
        report.surplus_rows_by_copies[n_copies] = report.surplus_rows_by_copies.get(n_copies, 0) + len(losers)
        report.empty_losers += len(empty_ids)
        report.source_only_losers += len(source_only_ids)
        report.non_empty_losers += len(non_empty_ids)
        for name, n in group_rows_by_table.items():
            report.loser_rows_by_table[name] += n
        report.movements_contained_total += contained
        report.movements_unioned_total += unioned
        report.diverging_groups += 1 if unioned else 0
        # Court and competencia are part of the key / filter, so this is 0 by
        # construction; it is computed from the rows to prove it on real data.
        if len({(r.court_id, r.competencia) for r in rows}) > 1:
            report.groups_spanning_court_or_competencia += 1
        report.groups_with_three_or_more_copies += 1 if n_copies >= 3 else 0
        changes_owner = winner.lawyer_id != firm_id
        report.groups_winner_changes_owner += 1 if changes_owner else 0

        report.groups.append(
            GroupReport(
                rol=rol,
                court_id=court_id,
                procedure=procedure,
                copies=n_copies,
                winner_id=winner.id,
                winner_lawyer_id=winner.lawyer_id,
                winner_changes_owner=changes_owner,
                loser_ids=[loser.id for loser in losers],
                empty_loser_ids=empty_ids,
                source_only_loser_ids=source_only_ids,
                non_empty_loser_ids=non_empty_ids,
                movements_contained=contained,
                movements_unioned=unioned,
                loser_rows_by_table=group_rows_by_table,
            )
        )

    report.empty_only_first_pass_rows = report.empty_losers
    report.estimate = _estimate(
        report.surplus_rows, report.source_only_losers + report.non_empty_losers
    )
    return report
