"""One-off backfill of the matriz de clasificación over all civil cases.

Loads the matriz reference tables ONCE (see ``MatrizMappingCache``) and
classifies every civil ``Case`` row, committing in batches with a progress
line, then prints the final distribution (by matriz and by origen) so the
run can be sanity-checked against the design doc's known ~52.6%
sin_detalle backlog.

Idempotent + safe to re-run — it's a pure recompute, no destructive step.

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/recalcular_matriz.py
"""
import os

os.environ.setdefault("ENVIRONMENT", "production")

BATCH_SIZE = 200


def _distribution(db, Case):
    from sqlalchemy import func

    by_matriz = dict(
        db.query(Case.matriz, func.count())
        .filter(Case.competencia == "civil")
        .group_by(Case.matriz)
        .all()
    )
    by_origen = dict(
        db.query(Case.matriz_origen, func.count())
        .filter(Case.competencia == "civil")
        .group_by(Case.matriz_origen)
        .all()
    )
    return by_matriz, by_origen


def main() -> None:
    from datetime import datetime

    from app.core.database import SessionLocal
    from app.models.case import Case
    from app.services.matriz_classifier import MatrizMappingCache, classify_case

    db = SessionLocal()
    try:
        mapping_cache = MatrizMappingCache.load(db)

        cases = db.query(Case).filter(Case.competencia == "civil").all()
        total = len(cases)
        print(f"recalculando matriz para {total} causas civiles...")

        done = 0
        for case in cases:
            result = classify_case(db, case, mapping_cache=mapping_cache)
            case.matriz = result.matriz
            case.matriz_etapa = result.matriz_etapa
            case.matriz_origen = result.origen
            case.matriz_computed_at = datetime.utcnow()
            done += 1
            if done % BATCH_SIZE == 0:
                db.commit()
                print(f"  ...{done}/{total}")
        db.commit()

        print(f"\ndone {done} causas.")
        by_matriz, by_origen = _distribution(db, Case)
        print("\ndistribución por matriz:")
        for k, v in sorted(by_matriz.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
            pct = (v / total * 100) if total else 0
            print(f"  {k!r:>12}: {v:>6} ({pct:.1f}%)")
        print("\ndistribución por origen:")
        for k, v in sorted(by_origen.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
            pct = (v / total * 100) if total else 0
            print(f"  {k!r:>20}: {v:>6} ({pct:.1f}%)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
