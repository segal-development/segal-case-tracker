"""Recalculate (or, with ``--dry-run``, only report) ``cases.contrato_estado`` and ``cases.es_activa``.

``--dry-run`` reports the counts per state (before -> after) and how many causas are activas, and writes
NOTHING (it never commits). Without it, only the cases whose state changed are written; ``updated_at`` and
every other column are left alone. Idempotent.

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/recalcular_contrato_estado.py --dry-run
    PYTHONPATH=$(pwd) poetry run python scripts/recalcular_contrato_estado.py

``compute`` is the testable core (injected session); ``main`` owns the real ``SessionLocal``.
"""
import argparse
import os

os.environ.setdefault("ENVIRONMENT", "production")


def compute(db, *, dry_run: bool) -> dict:
    """Recompute every case's contract state; persist the changes unless *dry_run*."""
    from app.services.contrato_estado import recompute_contrato_estado

    return recompute_contrato_estado(db, dry_run=dry_run)


def _print_report(result: dict, dry_run: bool) -> None:
    print(f"\n{'simulado' if dry_run else 'listo'}: {result['total']} causas, {result['cambiadas']} con cambios.")
    print("\ncontrato_estado (antes -> después):")
    for estado, despues in result["por_estado"].items():
        antes = result["antes_por_estado"].get(estado, 0)
        print(f"  {estado:>9}: antes {antes:>6} -> después {despues:>6} ({despues - antes:+d})")
    antes, despues = result["activas_antes"], result["activas_despues"]
    print(f"\ncausas activas: antes {antes} -> después {despues} ({despues - antes:+d})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recalcula (o, con --dry-run, solo reporta) el estado de contrato de las causas."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra los conteos por estado sin escribir NADA en la base.",
    )
    args = parser.parse_args()

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        print("DRY-RUN (no se escribe nada)..." if args.dry_run else "recalculando...")
        _print_report(compute(db, dry_run=args.dry_run), args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
