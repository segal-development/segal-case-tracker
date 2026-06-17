"""One-off recompute of the procedural-deadline engine over all detailed civil
cases, applying the current (fixed) classifier rules. Idempotent + safe to re-run.

Run:
    set -a; source .env.backfill; set +a
    PYTHONPATH=$(pwd) /tmp/poetryenv/bin/poetry run python scripts/recompute_deadlines.py
"""
import os

os.environ.setdefault("ENVIRONMENT", "production")


def _dist(db, Case):
    from sqlalchemy import func
    rows = (
        db.query(Case.procedural_state, func.count())
        .filter(Case.competencia == "civil", Case.procedural_state.isnot(None))
        .group_by(Case.procedural_state)
        .all()
    )
    sem = {
        v: db.query(Case).filter(Case.competencia == "civil", Case.semaforo == v).count()
        for v in ("rojo", "amarillo", "verde")
    }
    return dict(rows), sem


def main() -> None:
    from app.core.database import SessionLocal
    from app.models.case import Case
    from app.services.deadline_engine import DeadlineEngine

    db = SessionLocal()
    cases = (
        db.query(Case)
        .filter(Case.competencia == "civil", Case.last_detail_checked_at.isnot(None))
        .all()
    )
    print(f"recomputing {len(cases)} detailed civil cases...")

    before_state, before_sem = _dist(db, Case)
    print("BEFORE state:", before_state)
    print("BEFORE semaforo:", before_sem)

    done = 0
    for c in cases:
        DeadlineEngine.recompute_case(db, c)
        done += 1
        if done % 200 == 0:
            db.commit()
            print(f"  ...{done}/{len(cases)}")
    db.commit()

    after_state, after_sem = _dist(db, Case)
    print(f"\ndone {done} cases.")
    print("AFTER state:", after_state)
    print("AFTER semaforo:", after_sem)


if __name__ == "__main__":
    main()
