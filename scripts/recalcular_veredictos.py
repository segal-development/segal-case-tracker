"""Re-evaluate the verdict of closed plazo rows with the CURRENT rule.

CLI fina: la lógica vive en ``app.services.recalcular_veredictos``.

Dry-run por DEFECTO: sin ``--apply`` no escribe nada y reporta las transiciones
``antes -> después``. Con ``--apply`` actualiza SOLO las columnas ``verdict*`` de
las filas cerradas (superseded / expired), en silencio: sin recalcular causas,
sin alertas, sin tocar el semáforo ni ``status``. Las filas marcadas por un
auditor y las filas vigentes no se tocan; se reportan aparte.

Es idempotente y reversible sin respaldo: ``--margen 0`` reproduce la regla
anterior al margen de publicación; volver a correr sin ``--margen`` la restaura.

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``):
  PYTHONPATH=. poetry run python scripts/recalcular_veredictos.py
  PYTHONPATH=. poetry run python scripts/recalcular_veredictos.py --detalle
  PYTHONPATH=. poetry run python scripts/recalcular_veredictos.py --apply
"""
import argparse
import sys

from app.core.database import SessionLocal
from app.services.recalcular_veredictos import ejecutar


def _print_transiciones(titulo: str, datos: dict) -> None:
    print(f"  {titulo}")
    if not datos:
        print("    (ninguna)")
    for (antes, despues), n in sorted(datos.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {antes:<22} -> {despues:<22} {n:>6}")


def _reportar(resultado, detalle: bool) -> None:
    print("=== Filas cerradas con veredicto, re-evaluadas con la regla vigente ===")
    print(f"  evaluadas: {resultado.evaluadas}")
    print(f"  cambian: {len(resultado.cambios)}")
    print(f"  la regla ya no determina veredicto (se dejan como están): {resultado.sin_veredicto_nuevo}")
    print("\n=== Transiciones (antes -> después) ===")
    _print_transiciones("veredicto", resultado.transiciones)
    print("\n=== Aparte: NO se tocan ===")
    print(f"  marcadas por un auditor (con veredicto): {resultado.protegidas}")
    _print_transiciones("cambiarían si no estuvieran marcadas", resultado.protegidas_que_cambiarian)
    print(f"  vigentes (las resuelve el motor en su próximo recálculo): {resultado.vigentes}")
    if detalle:
        print("\n=== Detalle ===")
        for c in resultado.cambios:
            print(f"  deadline {c.deadline_id} (causa {c.case_id}): {c.antes} -> {c.despues}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Escribe los cambios (por defecto es dry-run).")
    parser.add_argument("--detalle", action="store_true", help="Lista cada fila que cambia.")
    parser.add_argument(
        "--margen", type=int, default=None,
        help="Margen de publicación en días corridos (default: el de la configuración; 0 lo desactiva).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        resultado = ejecutar(db, apply=args.apply, publication_margin_days=args.margen)
        _reportar(resultado, args.detalle)
        if resultado.apply:
            print(f"\nAplicado: {len(resultado.cambios)} filas actualizadas. Sin alertas, sin cambios de semáforo.")
        else:
            print("\nDRY-RUN: no se escribió nada en la base de datos. Ejecuta con --apply para aplicar.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
