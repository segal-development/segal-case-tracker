"""Informe de SOLO LECTURA de causas duplicadas en ``cases`` (T5a).

CLI fina: toda la lógica vive en ``app.services.dedup_cases_report``
(``build_dedup_report``); este script solo imprime el resumen y, si se pide,
escribe el informe completo en JSON.

Es de solo lectura POR CONSTRUCCIÓN: abre la sesión, corre el informe, hace
``db.rollback()`` y cierra. NO existe ``--apply`` ni ninguna ruta que escriba o
llame a ``run_case_merge``; la limpieza real es otra tarea (T5b) con backup y
visto bueno. El informe usa un número FIJO de consultas (no depende de la
cantidad de grupos), así que es apto para correr por el proxy de Cloud SQL.

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``):
  PYTHONPATH=. poetry run python scripts/dedup_cases_report.py
  PYTHONPATH=. poetry run python scripts/dedup_cases_report.py --sample 10 --json /tmp/dedup.json
"""
import argparse
import json
import sys

from app.core.database import SessionLocal
from app.services.dedup_cases_report import DedupReport, GroupReport, build_dedup_report

DEFAULT_SAMPLE = 5


def _pick_sample(report: DedupReport, n: int) -> list[GroupReport]:
    """First ``n`` groups, guaranteeing at least one diverging group when any exists."""
    if n <= 0:
        return []
    sample = report.groups[:n]
    if any(g.is_diverging for g in sample):
        return sample
    diverging = next((g for g in report.groups if g.is_diverging), None)
    if diverging is None:
        return sample
    return sample[: n - 1] + [diverging]


def _print_summary(report: DedupReport, sample: int) -> None:
    print("=== Causas duplicadas por (rol, tribunal, cuaderno) — SOLO LECTURA ===")
    print(f"  grupos duplicados: {report.total_groups}")
    print(f"  filas sobrantes (perdedoras): {report.surplus_rows}")
    for copies in sorted(report.groups_by_copies):
        print(
            f"    {copies} copias: {report.groups_by_copies[copies]} grupos, "
            f"{report.surplus_rows_by_copies[copies]} filas sobrantes"
        )

    print("\n=== Clasificación de perdedoras ===")
    print(f"  VACÍAS (0 filas en toda tabla hija): {report.empty_losers}  <- pasada 1, sin riesgo")
    print(f"  solo case_lawyer_source (avistamiento a trasladar al ganador): {report.source_only_losers}")
    print(f"  NO vacías (con historial/hitos/etc.): {report.non_empty_losers}")

    print("\n=== Filas hijas colgadas de perdedoras, por tabla ===")
    for table, n in report.loser_rows_by_table.items():
        print(f"  {table}: {n}")
    if report.hitos_on_losers_by_estado:
        print("  hitos en perdedoras por estado: " + ", ".join(
            f"{estado}={n}" for estado, n in sorted(report.hitos_on_losers_by_estado.items())
        ))

    print("\n=== Movimientos ===")
    print(f"  ya contenidos en el ganador (folio, descripción): {report.movements_contained_total}")
    print(f"  se UNIRÍAN al ganador: {report.movements_unioned_total} en {report.diverging_groups} grupos")

    print("\n=== Banderas de riesgo ===")
    print(f"  grupos con distinto tribunal/competencia (debe ser 0): {report.groups_spanning_court_or_competencia}")
    print(f"  grupos con 3+ copias: {report.groups_with_three_or_more_copies}")
    print(f"  grupos donde el ganador cambia de dueño: {report.groups_winner_changes_owner}")

    est = report.estimate
    print("\n=== Estimación del merge existente (run_case_merge) ===")
    print(f"  {est['formula']}")
    print(
        f"  todas las perdedoras: {est['losers']} x {est['queries_per_loser']} x "
        f"{est['round_trip_seconds']} s / 3600 = {est['hours_all_losers']:.2f} h"
    )
    print(
        f"  tras borrar las vacías: {est['losers_after_empty_pass']} x {est['queries_per_loser']} x "
        f"{est['round_trip_seconds']} s / 3600 = {est['hours_after_empty_pass']:.2f} h"
    )
    print(f"  primera pasada (solo vacías, cero pérdida de información): {report.empty_only_first_pass_rows} filas")

    examples = _pick_sample(report, sample)
    if examples:
        print(f"\n=== Muestra de {len(examples)} grupos ===")
        for g in examples:
            print(
                f"  {g.rol} tribunal={g.court_id} cuaderno={g.procedure!r}: ganador={g.winner_id}, "
                f"perdedoras={g.loser_ids} (vacías={g.empty_loser_ids}, no vacías={g.non_empty_loser_ids}), "
                f"movimientos contenidos={g.movements_contained}, a unir={g.movements_unioned}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", metavar="PATH", default=None, help="Escribe el informe completo en JSON.")
    parser.add_argument(
        "--sample", type=int, default=DEFAULT_SAMPLE,
        help=f"Cantidad de grupos de ejemplo a mostrar (default {DEFAULT_SAMPLE}; incluye uno divergente si existe).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = build_dedup_report(db)
    finally:
        db.rollback()  # read-only by construction: never leave a transaction open
        db.close()

    _print_summary(report, args.sample)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        print(f"\nInforme completo escrito en {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
