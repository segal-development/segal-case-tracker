"""Reporte y aplicación de la poda de causas sin cobertura comercial.

CLI fina: toda la lógica de negocio vive en ``app.services.poda``
(``clasificar_cartera`` / ``aplicar_poda`` / ``coberturas_por_causa``); este
script solo arma el reporte y, si se pide explícitamente, aplica la poda.

Dry-run por DEFECTO — nunca escribe nada salvo que se pase ``--apply``.

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``: el proyecto
no se instala como paquete en el venv, así que sin eso no resuelve ``app``):
  PYTHONPATH=. poetry run python scripts/poda_dry_run.py
  PYTHONPATH=. poetry run python scripts/poda_dry_run.py --limite 50 --dias-movimiento 60
  PYTHONPATH=. poetry run python scripts/poda_dry_run.py --apply --actor-rut 11111111-1
"""
import argparse
import sys

from app.core.database import SessionLocal
from app.services.poda import (
    DIAS_MOVIMIENTO_RECIENTE,
    aplicar_poda,
    clasificar_cartera,
    coberturas_por_causa,
)

CARATULADO_MAX = 60


def _caratulado(case) -> str:
    texto = f"{case.plaintiff or ''} / {case.defendant or ''}".strip(" /")
    if len(texto) > CARATULADO_MAX:
        return texto[: CARATULADO_MAX - 1] + "…"
    return texto


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Aplica la poda (por defecto es dry-run).")
    parser.add_argument("--limite", type=int, default=None, help="Máximo de causas a podar.")
    parser.add_argument(
        "--dias-movimiento", type=int, default=DIAS_MOVIMIENTO_RECIENTE,
        help=f"Ventana de movimiento reciente a excluir (default {DIAS_MOVIMIENTO_RECIENTE} días).",
    )
    parser.add_argument(
        "--incluir-con-movimiento", action="store_true",
        help="Incluye causas con movimiento reciente en la poda (fuera del camino seguro).",
    )
    parser.add_argument(
        "--actor-rut", default=None,
        help="RUT de quien ejecuta la poda — requerido con --apply.",
    )
    args = parser.parse_args()

    if args.apply and not args.actor_rut:
        print("ERROR: --actor-rut es requerido junto con --apply.")
        return 2

    db = SessionLocal()
    try:
        clasificacion = clasificar_cartera(db)

        print("=== Reparto de la cartera (causas no podadas) ===")
        print(f"  total causas evaluadas: {clasificacion.total_causas}")
        print(f"  podables (sin cobertura): {len(clasificacion.podables)}")
        print(f"  con cliente activo: {clasificacion.con_cliente_activo}")
        print(f"  con cliente moroso: {clasificacion.con_cliente_moroso}")
        print(f"  cobertura desconocida (sin dato / ausente del caché): {clasificacion.sin_parte_conocida}")
        print(f"  sin partes evaluables (sin litigantes con RUT): {clasificacion.sin_partes_evaluables}")

        if not clasificacion.podables:
            print("\nNo hay causas podables. Nada más que hacer.")
            return 0

        coberturas = coberturas_por_causa(db, clasificacion.podables)
        from app.models.case import Case  # local import: script-only dependency

        causas = (
            db.query(Case)
            .filter(Case.id.in_(clasificacion.podables))
            .order_by(Case.id)
            .all()
        )

        print(f"\n=== Causas podables ({len(causas)}) ===")
        header = f"{'case_id':>8}  {'rol':<18}  {'tribunal':<28}  {'caratulado':<{CARATULADO_MAX}}  {'abogado':<24}  {'last_movement_at':<19}  coberturas"
        print(header)
        print("-" * len(header))
        for case in causas:
            tribunal = (case.court.name if case.court else "") or ""
            abogado = (case.lawyer.name if case.lawyer else "") or ""
            last_mov = case.last_movement_at.isoformat(sep=" ", timespec="minutes") if case.last_movement_at else "—"
            partes = coberturas.get(case.id, {})
            partes_txt = ", ".join(f"{rut}={cob}" for rut, cob in sorted(partes.items())) or "—"
            print(
                f"{case.id:>8}  {case.rol:<18}  {tribunal[:28]:<28}  {_caratulado(case):<{CARATULADO_MAX}}  "
                f"{abogado[:24]:<24}  {last_mov:<19}  {partes_txt}"
            )

        resultado = aplicar_poda(
            db,
            actor_rut=args.actor_rut or "dry-run",
            dias_movimiento_reciente=args.dias_movimiento,
            incluir_con_movimiento=args.incluir_con_movimiento,
            limite=args.limite,
            dry_run=not args.apply,
        )

        print("\n=== Resumen ===")
        print(f"  podables: {resultado['podables']}")
        print(f"  podadas: {resultado['podadas']}")
        print(f"  salteadas por movimiento reciente: {resultado['salteadas_por_movimiento']}")

        if resultado["dry_run"]:
            print("\nDRY-RUN: no se escribió nada en la base de datos. Ejecuta con --apply para aplicar.")
        else:
            print(f"\nAplicado: {resultado['podadas']} causas marcadas como podadas (actor={args.actor_rut}).")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
