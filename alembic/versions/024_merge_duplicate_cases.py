"""merge duplicate ROL Case rows into one firm-owned Case (ADR-008 Slice 2)

Revision ID: 024
Revises: 023
Create Date: 2026-07-06

DATA migration — invokes ``app.services.case_merge.run_case_merge`` inside a
single transaction. Verify-before-delete: if post-merge invariants fail, the
whole transaction is rolled back (nothing is deleted, nothing stays
half-repointed).

*** THIS MIGRATION IS NOT APPLIED VIA `alembic upgrade` AGAINST LIVE QA. ***
Per this repo's existing convention (see docs/runbooks/unificar-modelo-
causas-migration.md), `alembic upgrade` does not reach the QA Cloud SQL
instance directly; 022/023/024 are applied there manually (direct DDL/data
run + `UPDATE alembic_version`) after taking a DB snapshot. Running this
`upgrade()` locally/in CI against the test DB is safe and expected (that is
how the Slice-2 test suite exercises it); it must NOT be run ad hoc against
QA outside the runbook procedure.

``downgrade()`` is a best-effort partial reversal via ``case_merge_audit``
(see ``app.services.case_merge.reverse_case_merge`` docstring for its
limitations) — the PRIMARY rollback path for the real QA run is restoring
the pre-migration DB snapshot, not this downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.orm import Session

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.services.case_merge import run_case_merge

    bind = op.get_bind()
    session = Session(bind=bind)
    try:
        result = run_case_merge(session)
        session.commit()
        print(  # noqa: T201 — surfaced in alembic upgrade output for the operator
            "case_merge: merged_rols="
            f"{result.merged_rol_count} losers_deleted={result.losers_deleted} "
            f"audit_rows={result.audit_rows_created} reowned={result.reowned_case_count}"
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def downgrade() -> None:
    from app.services.case_merge import reverse_case_merge

    bind = op.get_bind()
    session = Session(bind=bind)
    try:
        recreated = reverse_case_merge(session)
        session.commit()
        print(f"case_merge downgrade: recreated {recreated} skeleton loser Case row(s)")  # noqa: T201
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
