"""Import titulo_tipo (and optional titulo_fecha) from CSV and recompute prescripción.

CSV format: flexible header. REQUIRED column: tipo. OPTIONAL columns: rol, rut, nombre, fecha.
  - tipo:   required. pagare|letra|cheque|escritura_publica|sentencia|otro (stored lowercased)
  - rol:    case rol for exact match (civil competencia). Highest priority.
  - rut:    debtor's RUT — matched against all case_litigantes (any participante). May touch multiple cases.
  - nombre: debtor name — matched against case_litigantes with participante like "DDO%",
            OR against Case.defendant. Exact-normalized match only.
  - fecha:  OPTIONAL. YYYY-MM-DD or DD-MM-YYYY (both accepted). Stored into titulo_fecha.

Cascade match priority for each row: rol → rut → nombre. Stops at first level that yields a match.
If rol is present but finds no case, falls through to rut, then nombre.

RUT normalization: app.utils.rut.normalize_rut applied to BOTH the CSV value and the stored
case_litigantes.rut at compare time. Canonical form: {digits}-{DV} (no dots, uppercase K for DV).
Example: "12.345.678-9" and "12345678-9" both normalize to "12345678-9" → match.

Name normalization: lowercase, NFKD decompose + drop Mn category (strip accents), collapse whitespace.

Run:
    PYTHONPATH=$(pwd) <venv>/bin/python scripts/import_titulos.py <csv_file>

Idempotent (re-runnable).
"""
import os
import sys
import csv
import unicodedata
from datetime import date
from typing import Optional

os.environ.setdefault("ENVIRONMENT", "production")

from app.core.database import SessionLocal
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.services.deadline_engine import DeadlineEngine
from app.utils.rut import normalize_rut


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


def _normalize_rut_for_match(rut: str) -> str:
    """Normalize RUT for comparison. Uses app.utils.rut.normalize_rut.

    Removes dots and spaces, keeps hyphen, uppercases verification digit K.
    Result: '12345678-9' or '12345678-K'.
    Apply to BOTH sides (CSV and DB) before comparing.
    """
    return normalize_rut(rut.strip())


def _normalize_nombre(nombre: str) -> str:
    """Normalize a name for comparison: NFKD → strip Mn (accents) → lowercase → collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", nombre)
    without_accents = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return " ".join(without_accents.lower().split())


def _apply_to_cases(db, cases: list, tipo: str, fecha: Optional[date]) -> None:
    """Set titulo_tipo and titulo_fecha on each case and call DeadlineEngine.recompute_case."""
    for c in cases:
        c.titulo_tipo = tipo or None
        c.titulo_fecha = fecha
        DeadlineEngine.recompute_case(db, c)


def import_titulos(csv_path: str) -> dict:
    """Read CSV and update cases using cascade match (rol → rut → nombre). Returns summary dict."""

    db = SessionLocal()
    try:
        total_rows = 0
        matched_by_rol = 0
        matched_by_rut = 0
        rut_cases_touched = 0
        matched_by_name = 0
        name_cases_touched = 0
        unmatched: list[str] = []

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1
                rol = (row.get("rol") or "").strip()
                rut = (row.get("rut") or "").strip()
                nombre = (row.get("nombre") or "").strip()
                tipo = (row.get("tipo") or "").strip().lower()
                fecha_raw = (row.get("fecha") or "").strip()
                fecha = _parse_fecha(fecha_raw) if fecha_raw else None

                # --- Level 1: rol ---
                if rol:
                    cases = (
                        db.query(Case)
                        .filter(Case.rol == rol, Case.competencia == "civil")
                        .all()
                    )
                    if not cases:
                        cases = db.query(Case).filter(Case.rol == rol).all()
                    if cases:
                        _apply_to_cases(db, cases, tipo, fecha)
                        matched_by_rol += 1
                        continue

                # --- Level 2: rut ---
                if rut:
                    try:
                        norm_rut = _normalize_rut_for_match(rut)
                    except Exception:
                        norm_rut = rut  # fallback for malformed rut
                    all_litigantes = (
                        db.query(CaseLitigante)
                        .filter(CaseLitigante.rut != "")
                        .all()
                    )
                    case_ids = {
                        lit.case_id
                        for lit in all_litigantes
                        if lit.rut and _normalize_rut_for_match(lit.rut) == norm_rut
                    }
                    if case_ids:
                        cases = (
                            db.query(Case)
                            .filter(Case.id.in_(case_ids), Case.competencia == "civil")
                            .all()
                        )
                        if not cases:
                            cases = db.query(Case).filter(Case.id.in_(case_ids)).all()
                        if cases:
                            _apply_to_cases(db, cases, tipo, fecha)
                            matched_by_rut += 1
                            rut_cases_touched += len(cases)
                            continue

                # --- Level 3: nombre ---
                if nombre:
                    norm_nombre = _normalize_nombre(nombre)
                    all_ddo = (
                        db.query(CaseLitigante)
                        .filter(CaseLitigante.participante.like("DDO%"))
                        .all()
                    )
                    lit_case_ids = {
                        lit.case_id
                        for lit in all_ddo
                        if _normalize_nombre(lit.nombre) == norm_nombre
                    }
                    # Fetch all civil cases; match on lit_case_ids OR Case.defendant
                    all_civil = (
                        db.query(Case)
                        .filter(Case.competencia == "civil")
                        .all()
                    )
                    cases = [
                        c for c in all_civil
                        if c.id in lit_case_ids
                        or (c.defendant and _normalize_nombre(c.defendant) == norm_nombre)
                    ]
                    if cases:
                        _apply_to_cases(db, cases, tipo, fecha)
                        matched_by_name += 1
                        name_cases_touched += len(cases)
                        continue

                # --- No match ---
                label = rol or rut or nombre or f"row-{total_rows}"
                unmatched.append(label)

        db.commit()
        total_cases_updated = matched_by_rol + rut_cases_touched + name_cases_touched
        return {
            "total_rows": total_rows,
            "matched_by_rol": matched_by_rol,
            "matched_by_rut": matched_by_rut,
            "rut_cases_touched": rut_cases_touched,
            "matched_by_name": matched_by_name,
            "name_cases_touched": name_cases_touched,
            "unmatched": unmatched,
            # backward-compat keys used by existing tests
            "matched": total_cases_updated,
            "updated": total_cases_updated,
            "not_found": unmatched,
        }
    finally:
        db.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_titulos.py <csv_file>")
        sys.exit(1)
    csv_path = sys.argv[1]
    result = import_titulos(csv_path)
    print(f"Total rows processed: {result['total_rows']}")
    print(f"Matched by rol:  {result['matched_by_rol']}")
    print(f"Matched by rut:  {result['matched_by_rut']} rows → {result['rut_cases_touched']} cases")
    print(f"Matched by name: {result['matched_by_name']} rows → {result['name_cases_touched']} cases")
    if result["unmatched"]:
        print(f"Unmatched ({len(result['unmatched'])} rows):")
        for r in result["unmatched"][:10]:
            print(f"  - {r}")
        if len(result["unmatched"]) > 10:
            print(f"  ... and {len(result['unmatched']) - 10} more")
    else:
        print("All rows matched.")


if __name__ == "__main__":
    main()
