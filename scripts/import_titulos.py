"""Import titulo_tipo + titulo_fecha from CSV and recompute prescripción.

CSV format: rol,tipo,fecha
  - fecha: YYYY-MM-DD or DD-MM-YYYY (both accepted)
  - tipo: pagare|letra|cheque|escritura_publica|sentencia|otro (stored lowercased)

Run:
    PYTHONPATH=$(pwd) <venv>/bin/python scripts/import_titulos.py <csv_file>

Idempotent (re-runnable). Matches civil cases first; falls back to all competencias.
"""
import os
import sys
import csv
from datetime import date
from typing import Optional

os.environ.setdefault("ENVIRONMENT", "production")

from app.core.database import SessionLocal
from app.models.case import Case
from app.services.deadline_engine import DeadlineEngine


def _parse_fecha(raw: str) -> Optional[date]:
    """Accept YYYY-MM-DD or DD-MM-YYYY."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def import_titulos(csv_path: str) -> dict:
    """Read CSV and update cases. Returns summary dict."""

    db = SessionLocal()
    try:
        matched = 0
        updated = 0
        not_found: list[str] = []

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rol = (row.get("rol") or "").strip()
                tipo = (row.get("tipo") or "").strip().lower()
                fecha_raw = (row.get("fecha") or "").strip()

                if not rol:
                    continue

                fecha = _parse_fecha(fecha_raw)

                # Civil first, then all competencias
                cases = (
                    db.query(Case)
                    .filter(Case.rol == rol, Case.competencia == "civil")
                    .all()
                )
                if not cases:
                    cases = db.query(Case).filter(Case.rol == rol).all()

                if not cases:
                    not_found.append(rol)
                    continue

                matched += len(cases)
                for c in cases:
                    c.titulo_tipo = tipo or None
                    c.titulo_fecha = fecha
                    DeadlineEngine.recompute_case(db, c)
                    updated += 1

        db.commit()
        return {"matched": matched, "updated": updated, "not_found": not_found}
    finally:
        db.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_titulos.py <csv_file>")
        sys.exit(1)
    csv_path = sys.argv[1]
    result = import_titulos(csv_path)
    print(f"Matched: {result['matched']} cases")
    print(f"Updated: {result['updated']} cases")
    if result["not_found"]:
        print(f"Not found ({len(result['not_found'])} rols):")
        for r in result["not_found"]:
            print(f"  - {r}")
    else:
        print("All rols matched.")


if __name__ == "__main__":
    main()
