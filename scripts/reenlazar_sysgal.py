"""One-off backfill: link ACTIVE Sysgal causas that are still unlinked to our cases.

The daily sync only re-matches Sysgal rows changed since ``actualizado_desde`` (and now also
re-matches every unlinked active row after each sync). This script runs that same re-match
once for prod, then reclassifies the matriz of the cases whose Sysgal input changed.

``--dry-run`` writes NOTHING and reports how many causas would link. Output is counts only
(no Sysgal payload).

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/reenlazar_sysgal.py --dry-run
    PYTHONPATH=$(pwd) poetry run python scripts/reenlazar_sysgal.py

``run`` is the testable core (session injected); ``main`` owns the real session.
"""
import argparse
import os

os.environ.setdefault("ENVIRONMENT", "production")


def run(db, *, dry_run: bool) -> dict:
    """Re-match unlinked active causas; unless *dry_run*, persist links and reclassify affected cases."""
    from app.services.matriz_sysgal import load_sysgal_inputs, reclassify_cases
    from app.services.sysgal_causas import rematch_unlinked

    before = load_sysgal_inputs(db)
    report = rematch_unlinked(db, dry_run=dry_run)
    report["reclasificadas"] = 0
    if dry_run:
        db.rollback()
        return report
    db.commit()
    after = load_sysgal_inputs(db)
    changed = {cid for cid in before.keys() | after.keys() if before.get(cid) != after.get(cid)}
    report["reclasificadas"] = reclassify_cases(db, changed)
    return report


def format_report(report: dict, *, dry_run: bool) -> str:
    head = "DRY-RUN (no se escribe nada)" if dry_run else "aplicado"
    verb = "se enlazarían" if dry_run else "enlazadas"
    lines = [
        f"{head}: re-enlace de causas Sysgal activas sin caso",
        f"  candidatas (activas, sin caso): {report['candidatas']}",
        f"  {verb}: {report['reenlazadas']}",
        f"  ambiguas (quedan sin enlazar): {report['ambiguas']}",
        f"  sin calce: {report['sin_calce']}",
    ]
    if not dry_run:
        lines.append(f"  casos con matriz recalculada: {report['reclasificadas']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enlaza causas Sysgal activas que quedaron sin caso.")
    parser.add_argument("--dry-run", action="store_true", help="Solo reporta cuántas se enlazarían; no escribe.")
    args = parser.parse_args()

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        print(format_report(run(db, dry_run=args.dry_run), dry_run=args.dry_run))
    finally:
        db.close()


if __name__ == "__main__":
    main()
