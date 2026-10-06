"""Crea las filas de plazo ``excepciones_8d`` que faltan (backfill).

CLI fina: la lógica vive en ``app.services.backfill_plazos_excepciones``.

Dry-run por DEFECTO: sin ``--apply`` no escribe nada y reporta cuántas filas
crearía, por estado de la causa y por veredicto. Con ``--apply`` las crea EN
SILENCIO: no recalcula causas, no emite alertas, no mueve el semáforo (ver el
docstring del servicio). Cada fila queda marcada con ``origin`` para poder
deshacerlas con ``--revertir --apply``.

Alcance (``--alcance``): ``activas`` (default, el más conservador) o ``todas``
(incluye archivadas y terminadas).

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``):
  PYTHONPATH=. poetry run python scripts/backfill_plazos_excepciones.py
  PYTHONPATH=. poetry run python scripts/backfill_plazos_excepciones.py --alcance todas
  PYTHONPATH=. poetry run python scripts/backfill_plazos_excepciones.py --apply
  PYTHONPATH=. poetry run python scripts/backfill_plazos_excepciones.py --revertir --apply
"""
import argparse
import sys

from app.core.database import SessionLocal
from app.services.backfill_plazos_excepciones import (
    ORIGIN,
    Alcance,
    contar_reversion,
    ejecutar,
    revertir,
)


def _print_counter(titulo: str, datos: dict) -> None:
    print(f"  {titulo}")
    if not datos:
        print("    (ninguna)")
    for clave, n in sorted(datos.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"    {str(clave):<28} {n:>6}")


def _reportar(resultado) -> None:
    plan = resultado.plan
    print("=== Causas con excepciones presentadas y sin fila de plazo ===")
    print(f"  total: {plan.con_presentacion_sin_fila}")
    print(f"  se les puede crear fila: {plan.a_crear_total}")
    print(f"  sin ancla (no hay notificación desde la que contar; NO reciben fila): {plan.sin_ancla_total}")
    print(f"  no civiles (el motor no les calcula plazos): {plan.no_civil}")
    print(f"  plazo vigente para el motor (lo crea el motor en su próximo recálculo): {plan.plazo_vigente_en_motor}")
    print(f"  aparte: con fila obsoleta sin veredicto (el endpoint sigue en sin_ancla; no se tocan): "
          f"{plan.con_fila_obsoleta_sin_veredicto}")

    print("\n=== Filas a crear, por estado de la causa ===")
    _print_counter("estado (Case.status)", plan.a_crear_por_estado)
    _print_counter("estado procesal", plan.a_crear_por_estado_procesal)
    print("\n=== Filas a crear, por veredicto ===")
    _print_counter("veredicto", plan.a_crear_por_veredicto)
    print("\n=== Estado x veredicto ===")
    _print_counter("(estado, veredicto)", plan.a_crear_por_estado_y_veredicto)
    print("\n=== Sin ancla, por estado de la causa ===")
    _print_counter("estado", plan.sin_ancla_por_estado)

    print(f"\n=== Alcance elegido: {resultado.alcance.value} ===")
    print(f"  filas dentro del alcance: {resultado.en_alcance}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Escribe los cambios (por defecto es dry-run).")
    parser.add_argument(
        "--alcance", choices=[a.value for a in Alcance], default=Alcance.ACTIVAS.value,
        help="activas (default, conservador) o todas (incluye archivadas y terminadas).",
    )
    parser.add_argument(
        "--revertir", action="store_true",
        help=f"Deshace lo creado por este backfill (filas con origin={ORIGIN}). Necesita --apply para borrar.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.revertir:
            if args.apply:
                r = revertir(db)
                print(f"Revertido: {r.eliminadas} filas eliminadas; {r.conservadas} conservadas (alguien las tocó después).")
            else:
                r = contar_reversion(db)
                print(f"DRY-RUN: se eliminarían {r.eliminadas} filas; se conservarían {r.conservadas} (tocadas después).")
                print("No se escribió nada. Ejecuta con --revertir --apply para deshacer.")
            return 0

        resultado = ejecutar(db, apply=args.apply, alcance=Alcance(args.alcance))
        _reportar(resultado)
        if resultado.apply:
            print(f"\nAplicado: {len(resultado.creadas)} filas creadas (origin={ORIGIN}). "
                  "Sin alertas, sin cambios de semáforo.")
        else:
            print("\nDRY-RUN: no se escribió nada en la base de datos. Ejecuta con --apply para aplicar.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
