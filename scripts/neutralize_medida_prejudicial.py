"""Neutraliza las causas con procedimiento 'Medida prejudicial' (backfill).

En esas causas el estudio es siempre el solicitante y no corre el flujo del
juicio ejecutivo: no llevan semáforo de color, plazos, recomendaciones ni
alertas. El motor ya las deja neutras en cada recálculo; este script aplica lo
mismo a las filas EXISTENTES con datos viejos (semáforo rojo/amarillo, plazo
fatal, filas de ``case_deadlines`` vencidas).

Dry-run por DEFECTO: sin ``--apply`` solo imprime conteos (SELECT agregados) y no
escribe nada. Con ``--apply`` neutraliza en lotes de 200 con un commit por lote.
No emite alertas, no borra plazos (los no manuales quedan ``superseded`` con
veredicto NULL; los manuales no se tocan) y no toca las Alert ya guardadas.
Reanudable: solo procesa las filas que todavía no están neutras.

Uso (``PYTHONPATH=.`` es la convención del repo para ``scripts/``):
  PYTHONPATH=. poetry run python scripts/neutralize_medida_prejudicial.py
  PYTHONPATH=. poetry run python scripts/neutralize_medida_prejudicial.py --apply
"""
import argparse
import sys
from typing import Optional, Sequence

from sqlalchemy import exists, func, or_

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.services.deadline_engine import DeadlineEngine, medida_prejudicial_clause

BATCH_SIZE = 200


def _open_session():
    # Lazy: importing this module (or --help) must not touch the database layer.
    from app.core.database import SessionLocal

    return SessionLocal()


def _pending_deadlines_clause():
    """Non-manual plazo rows that are not yet ``superseded`` with a NULL verdict."""
    return (
        CaseDeadline.is_manual == False,  # noqa: E712
        or_(CaseDeadline.status != "superseded", CaseDeadline.verdict.isnot(None)),
    )


def _not_neutral_clause():
    """Cases whose stored state is not the neutral one (or that keep stale plazos)."""
    stale_plazos = exists().where(
        CaseDeadline.case_id == Case.id, *_pending_deadlines_clause()
    )
    return or_(
        Case.semaforo.is_(None),
        Case.semaforo != "gris",
        Case.procedural_state.is_(None),
        Case.procedural_state != "indeterminate",
        Case.next_deadline_at.isnot(None),
        Case.abandono_disponible.is_(True),
        Case.next_deadline_fatal.is_(True),
        Case.prescripcion_cumplida.is_(True),
        Case.plazo_fatal_incumplido.is_(True),
        Case.en_apremio.is_(True),
        Case.prescripcion_fecha.isnot(None),
        Case.recommended_action_code.isnot(None),
        Case.next_review_at.isnot(None),
        Case.semaforo_override.isnot(None),
        Case.semaforo_override_at.isnot(None),
        Case.semaforo_override_by.isnot(None),
        stale_plazos,
    )


def _count(db, *criteria) -> int:
    return db.query(func.count(Case.id)).filter(medida_prejudicial_clause(), *criteria).scalar()


def _report(db) -> int:
    """Print the counts (aggregate SELECTs only). Returns the not-neutral rows."""
    total = _count(db)
    pending = _count(db, _not_neutral_clause())
    colored = _count(
        db, or_(Case.semaforo.in_(("rojo", "amarillo")), Case.next_deadline_fatal.is_(True))
    )
    plazos = (
        db.query(func.count(CaseDeadline.id))
        .join(Case, Case.id == CaseDeadline.case_id)
        .filter(medida_prejudicial_clause(), *_pending_deadlines_clause())
        .scalar()
    )
    print(f"filas medida prejudicial: {total}")
    print(f"no neutras: {pending}")
    print(f"plazos a reemplazar: {plazos}")
    print(f"rojo/amarillo/fatal: {colored}")
    return pending


def _apply(db) -> int:
    """Neutralize the pending rows in batches, one commit per batch."""
    done = 0
    last_id = 0
    while True:
        batch = (
            db.query(Case)
            .filter(medida_prejudicial_clause(), _not_neutral_clause(), Case.id > last_id)
            .order_by(Case.id)
            .limit(BATCH_SIZE)
            .all()
        )
        if not batch:
            return done
        try:
            for case in batch:
                DeadlineEngine.neutralize_medida_prejudicial(db, case)
            db.commit()
        except Exception:
            db.rollback()
            raise
        done += len(batch)
        last_id = batch[-1].id
        print(f"  neutralizadas: {done}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true", help="Escribe los cambios (por defecto es dry-run)."
    )
    args = parser.parse_args(argv)

    db = _open_session()
    try:
        print("=== Medida prejudicial: estado actual ===")
        _report(db)
        if not args.apply:
            print("\nDry-run: no se escribió nada. Use --apply para neutralizar.")
            return 0
        print("\n=== Aplicando ===")
        done = _apply(db)
        print(f"\nListo: {done} filas neutralizadas.")
        print("\n=== Medida prejudicial: estado final ===")
        _report(db)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
