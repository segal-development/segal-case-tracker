"""Recompute the semáforo of every detailed civil case (and never-detailed ones with abandono available) (Fase 0 auditoría) and report what changes.

Default is a DRY-RUN: the engine runs inside a transaction that is rolled back (a
``before_commit`` guard makes a commit impossible), so NOTHING is written. Pass ``--apply``
to persist (commits in batches; also creates the escalation alerts, idempotently).

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/recompute_semaforo.py --dry-run
    PYTHONPATH=$(pwd) poetry run python scripts/recompute_semaforo.py --apply

Output (dry-run): first line ``DRY-RUN (nada se escribió)`` (``APLICADO`` with --apply), then
``key: value`` lines: causas recalculadas, cambian de color, verde→rojo por abandono,
→rojo por plazo fatal vencido, alertas de escalamiento a crear, then the before/after
distribution. ``compute_report`` is the testable core (takes an injected session).
"""
from __future__ import annotations

import argparse
import os
from collections import Counter

os.environ.setdefault("ENVIRONMENT", "production")

BATCH_SIZE = 200
_ABANDONO = "Abandono disponible"
_VENCIDO = "Plazo fatal vencido sin escrito"


def compute_report(db, *, dry_run: bool) -> dict:
    from sqlalchemy import event, or_

    from app.models.case import Case
    from app.services.deadline_engine import DeadlineEngine

    def _no_commit(session):  # pragma: no cover - safety net
        if session.in_nested_transaction():
            return  # savepoint release (engine safe-fail blocks): nothing is persisted
        raise RuntimeError("dry-run: commit is not allowed")

    if dry_run:
        event.listen(db, "before_commit", _no_commit)

    ids = [
        row[0]
        for row in db.query(Case.id)
        .filter(
            Case.competencia == "civil",
            # Detailed cases, plus never-detailed ones that already flag an available abandono
            # (they must not stay green; the engine works from movements, not from the detail).
            or_(Case.last_detail_checked_at.isnot(None), Case.abandono_disponible.is_(True)),
        )
        .order_by(Case.id)
        .all()
    ]
    before: Counter = Counter()
    after: Counter = Counter()
    report = {
        "dry_run": dry_run, "total": len(ids), "changed": 0,
        "abandono_verde_a_rojo": 0, "vencido_a_rojo": 0, "alerts_would_create": 0,
    }
    try:
        for start in range(0, len(ids), BATCH_SIZE):
            for case in db.query(Case).filter(Case.id.in_(ids[start:start + BATCH_SIZE])).all():
                old = case.semaforo
                transition = DeadlineEngine.recompute_case(db, case)
                new = case.semaforo
                before[old] += 1
                after[new] += 1
                report["alerts_would_create"] += transition.escalations_created
                if old != new:
                    report["changed"] += 1
                    motivo = case.semaforo_motivo or ""
                    if new == "rojo" and motivo.startswith(_VENCIDO):
                        report["vencido_a_rojo"] += 1
                    elif old == "verde" and new == "rojo" and motivo.startswith(_ABANDONO):
                        report["abandono_verde_a_rojo"] += 1
            if dry_run:
                db.rollback()
            else:
                db.commit()
    finally:
        if dry_run:
            db.rollback()
            event.remove(db, "before_commit", _no_commit)
    report["before"] = dict(before)
    report["after"] = dict(after)
    return report


def format_report(r: dict) -> str:
    return "\n".join([
        "DRY-RUN (nada se escribió)" if r["dry_run"] else "APLICADO",
        f"causas recalculadas: {r['total']}",
        f"cambian de color: {r['changed']}",
        f"verde→rojo por abandono: {r['abandono_verde_a_rojo']}",
        f"→rojo por plazo fatal vencido: {r['vencido_a_rojo']}",
        f"alertas de escalamiento a crear: {r['alerts_would_create']}",
        f"semáforo antes: {r['before']}",
        f"semáforo después: {r['after']}",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute the semáforo (Fase 0 auditoría)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="report only (default)")
    group.add_argument("--apply", action="store_true", help="persist the recompute and the alerts")
    args = parser.parse_args()

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        print(format_report(compute_report(db, dry_run=not args.apply)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
