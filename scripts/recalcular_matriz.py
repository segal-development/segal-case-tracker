"""Recalculate (or, with ``--dry-run``, only report) the matriz de
clasificación over all civil cases.

Loads the matriz reference tables ONCE (see ``MatrizMappingCache``) and
classifies every civil ``Case`` row, printing the resulting distribution (by
matriz and by origen) plus the top unmapped stages/descriptions — the same
report ``GET /matriz/distribucion`` exposes, so the two never drift apart.

``--dry-run`` is the first-class tool for validating a mapping change (or
the classifier itself) against Sysgal's numbers BEFORE committing anything:
it never calls ``db.commit()`` and never mutates any ``Case`` attribute, so
it is safe to run read-only against a live database.

Without ``--dry-run``, it also persists the result on each ``Case``,
committing in batches with a progress line. Idempotent + safe to re-run.

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/recalcular_matriz.py --dry-run
    PYTHONPATH=$(pwd) poetry run python scripts/recalcular_matriz.py

``compute_distribution`` below is the testable core (takes an injected DB
session) — see ``tests/scripts/test_recalcular_matriz.py``. ``main`` is a
thin CLI wrapper that owns the real ``SessionLocal`` session.
"""
import argparse
import os
from collections import Counter

os.environ.setdefault("ENVIRONMENT", "production")

BATCH_SIZE = 200
TOP_UNMAPPED_LIMIT = 20


def compute_distribution(db, *, dry_run: bool) -> dict:
    """Classify every civil ``Case``, persisting unless *dry_run*.

    Returns ``{"total", "done", "by_matriz", "by_origen", "no_mapeada_ids"}``.
    Never calls ``db.commit()`` and never mutates a ``Case`` attribute when
    ``dry_run=True`` — safe to run read-only against a live database.
    """
    from datetime import datetime

    from app.models.case import Case
    from app.services.matriz_classifier import MatrizMappingCache, classify_case

    mapping_cache = MatrizMappingCache.load(db)
    cases = db.query(Case).filter(Case.competencia == "civil").all()
    total = len(cases)

    by_matriz: Counter = Counter()
    by_origen: Counter = Counter()
    no_mapeada_ids: list[int] = []

    done = 0
    for case in cases:
        result = classify_case(db, case, mapping_cache=mapping_cache)
        by_matriz[result.matriz] += 1
        by_origen[result.origen] += 1
        if result.origen == "no_mapeada":
            no_mapeada_ids.append(case.id)

        if not dry_run:
            case.matriz = result.matriz
            case.matriz_etapa = result.matriz_etapa
            case.matriz_origen = result.origen
            case.matriz_computed_at = datetime.utcnow()

        done += 1
        if not dry_run and done % BATCH_SIZE == 0:
            db.commit()

    if dry_run:
        # Belt-and-suspenders: discard any pending session state even though
        # nothing above mutated a Case attribute in dry-run mode.
        db.rollback()
    else:
        db.commit()

    return {
        "total": total,
        "done": done,
        "by_matriz": by_matriz,
        "by_origen": by_origen,
        "no_mapeada_ids": no_mapeada_ids,
    }


def _print_distribution(by_matriz: Counter, by_origen: Counter, total: int) -> None:
    print("\ndistribución por matriz:")
    for k, v in sorted(by_matriz.items(), key=lambda kv: (-kv[1], str(kv[0] or ""))):
        pct = (v / total * 100) if total else 0
        print(f"  {k!r:>12}: {v:>6} ({pct:.1f}%)")
    print("\ndistribución por origen:")
    for k, v in sorted(by_origen.items(), key=lambda kv: (-kv[1], str(kv[0] or ""))):
        pct = (v / total * 100) if total else 0
        print(f"  {k!r:>20}: {v:>6} ({pct:.1f}%)")


def _print_top_unmapped(db, no_mapeada_ids: list[int], total: int) -> None:
    from app.services.matriz_classifier import top_unmapped_movements

    unmapped = top_unmapped_movements(db, no_mapeada_ids, limit=TOP_UNMAPPED_LIMIT)
    if not unmapped:
        return
    print(
        f"\ntop {TOP_UNMAPPED_LIMIT} etapas/descripciones sin mapear "
        f"(de {len(no_mapeada_ids)} causas no_mapeada):"
    )
    for stage, descripcion, n in unmapped:
        label = stage if stage else f"(sin etapa) {descripcion or '(sin descripción)'}"
        pct = (n / total * 100) if total else 0
        print(f"  {label!r:>90}: {n:>6} ({pct:.1f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recalcula (o, con --dry-run, solo reporta) la matriz de clasificación."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calcula y muestra la distribución sin escribir NADA en la base (seguro contra la base real).",
    )
    args = parser.parse_args()

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        mode = "DRY-RUN (no se escribe nada)" if args.dry_run else "recalculando"
        print(f"{mode}: causas civiles...")

        result = compute_distribution(db, dry_run=args.dry_run)

        print(f"\n{'simulado' if args.dry_run else 'listo'}: {result['done']}/{result['total']} causas.")
        _print_distribution(result["by_matriz"], result["by_origen"], result["total"])
        _print_top_unmapped(db, result["no_mapeada_ids"], result["total"])
    finally:
        db.close()


if __name__ == "__main__":
    main()
