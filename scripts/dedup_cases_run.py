"""Ejecutor por lotes de la limpieza de causas duplicadas en ``cases`` (T5b2).

CLI fina sobre ``app.services.dedup_cases_runner.run_batches``.

POR DEFECTO ES UN DRY-RUN: sin ``--apply`` no escribe nada y no llama a
``run_case_merge``; solo imprime el plan y el comando de la primera corrida real.

Para ``--apply`` hacen falta TRES cosas, a propósito:
  1. El worker DETENIDO (el ejecutor se niega si hubo movimientos o sync_history
     en los últimos 10 min; ``--i-know-the-worker-is-running`` lo desactiva).
  2. Un backup verificado: ``--backup-ref <id del backup>`` es obligatorio.
  3. Escribir la palabra MERGE cuando el script lo pida (``--yes`` la omite,
     pero sigue exigiendo ``--backup-ref``).

Cada lote es una transacción propia; el primer error revierte SOLO ese lote y
detiene la corrida. Volver a correr reanuda desde el mismo grupo. Detención
elegante entre lotes: crear el archivo de ``--stop-file``.

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``):
  PYTHONPATH=. poetry run python scripts/dedup_cases_run.py --batch-size 5
  PYTHONPATH=. poetry run python scripts/dedup_cases_run.py --batch-size 5 --max-batches 1 \\
      --apply --backup-ref <ID_BACKUP>
"""
import argparse
import json
import sys
from typing import Optional

from app.services.dedup_cases_runner import RunnerRefused, RunSummary, run_batches

FIRST_APPLY_BATCH_SIZE = 5
OK_STOPS = {"dry_run", "completed", "max_batches", "stop_file", "time_budget"}


def _open_session():
    # Lazy: importing this module or running --help must not open a database.
    from app.core.database import SessionLocal

    return SessionLocal()


def _print_plan(summary: RunSummary) -> None:
    plan = summary.plan or {}
    print("=== Plan (SOLO LECTURA: no se escribió nada) ===")
    print(f"  grupos duplicados: {plan.get('groups')}")
    print(f"  lotes necesarios con --batch-size {plan.get('batch_size')}: {plan.get('batches_needed')}")
    print(f"  grupos del primer lote: {len(plan.get('first_batch_keys', []))}")
    for rol, court_id, procedure in plan.get("first_batch_keys", [])[:10]:
        print(f"    {rol} tribunal={court_id} cuaderno={procedure!r}")


def _print_first_apply_command() -> None:
    print("\nPrimera corrida real recomendada (un lote chico, para medir el tiempo):")
    print(
        "  PYTHONPATH=. poetry run python scripts/dedup_cases_run.py "
        f"--batch-size {FIRST_APPLY_BATCH_SIZE} --max-batches 1 --apply --backup-ref <ID_BACKUP>"
    )
    print("Requiere el worker detenido y el backup verificado. Ver docs/runbooks/dedup-cases-cleanup.md")


def _print_result(summary: RunSummary) -> None:
    print("\n=== Resultado ===")
    print(f"  modo: {summary.mode}")
    print(f"  lotes: {summary.batches_done}, grupos fusionados: {summary.groups_merged}, "
          f"perdedoras borradas: {summary.losers_deleted}")
    print(f"  grupos duplicados: antes={summary.groups_before} después={summary.remaining_groups}")
    print(f"  detenido por: {summary.stopped_because}")
    if summary.avg_seconds_per_group is not None:
        print(f"  segundos por grupo (promedio): {summary.avg_seconds_per_group:.2f}")
        print(f"  estimado para los restantes: {(summary.estimated_seconds_remaining or 0) / 3600:.2f} h")
    for error in summary.errors:
        print(f"  ERROR: {error}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-size", type=int, required=True, metavar="N", help="Grupos por lote (obligatorio).")
    parser.add_argument("--max-batches", type=int, default=None, metavar="N", help="Detener tras N lotes.")
    parser.add_argument("--apply", action="store_true", help="Ejecutar la fusión de verdad (default: dry-run).")
    parser.add_argument("--backup-ref", default=None, metavar="TEXT", help="Id del backup verificado (obligatorio con --apply).")
    parser.add_argument("--stop-file", default=None, metavar="PATH", help="Si existe, detiene la corrida entre lotes.")
    parser.add_argument("--time-budget-minutes", type=float, default=None, metavar="N", help="Presupuesto de tiempo; se chequea entre lotes.")
    parser.add_argument("--json", metavar="PATH", default=None, help="Escribe el resumen completo en JSON.")
    parser.add_argument("--yes", action="store_true", help="Omite la confirmación MERGE (sigue exigiendo --backup-ref).")
    parser.add_argument(
        "--i-know-the-worker-is-running", action="store_true", dest="worker_running",
        help="PELIGROSO: desactiva la guarda de base quieta.",
    )
    args = parser.parse_args(argv)

    if args.batch_size < 1:
        parser.error("--batch-size debe ser >= 1")
    if args.apply and not (args.backup_ref and args.backup_ref.strip()):
        parser.error("--apply requiere --backup-ref con el id del backup verificado")

    budget_s = args.time_budget_minutes * 60 if args.time_budget_minutes is not None else None
    common = dict(batch_size=args.batch_size, max_batches=args.max_batches, backup_ref=args.backup_ref)

    plan = run_batches(_open_session, **common)
    _print_plan(plan)
    if not args.apply:
        _print_first_apply_command()
        _write_json(args.json, plan)
        return 0

    if args.worker_running:
        print("\n!!! ADVERTENCIA: --i-know-the-worker-is-running desactiva la guarda de base quieta. !!!")
        print("!!! Un sync concurrente puede perder movimientos de las filas que se borran.     !!!")
    first = len((plan.plan or {}).get("first_batch_keys", []))
    print(f"\nEl primer lote va a FUSIONAR {first} grupos (backup: {args.backup_ref}).")
    if not args.yes:
        if input("Escribí MERGE para continuar: ").strip() != "MERGE":
            print("Confirmación incorrecta. No se escribió nada.")
            return 1

    try:
        summary = run_batches(
            _open_session,
            apply=True,
            stop_file=args.stop_file,
            time_budget_s=budget_s,
            require_quiescent=not args.worker_running,
            **common,
        )
    except RunnerRefused as exc:
        print(f"\nRECHAZADO (no se escribió nada): {exc}")
        return 1

    _print_result(summary)
    _write_json(args.json, summary)
    return 0 if summary.stopped_because in OK_STOPS else 1


def _write_json(path: Optional[str], summary: RunSummary) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary.to_dict(), fh, ensure_ascii=False, indent=2)
        print(f"\nResumen completo escrito en {path}")


if __name__ == "__main__":
    sys.exit(main())
