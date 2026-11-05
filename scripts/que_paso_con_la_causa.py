"""Answer, for one ROL or RUT, why the system does not show what the client reports.

A client calls about a movement we do not have. Searching the app and finding
nothing says nothing: the case may be missing, assigned to someone else, or
present but not refreshed in weeks. Each of those is a different problem with a
different fix. This prints which one it is.

READ-ONLY. Never writes.

Usage:
    ROL=C-10042-2026 PYTHONPATH=. <venv>/python scripts/que_paso_con_la_causa.py
    RUT=12345678-9   PYTHONPATH=. <venv>/python scripts/que_paso_con_la_causa.py
"""
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import or_

from app.core.database import SessionLocal
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement


def _dias(ts) -> str:
    if ts is None:
        return "nunca"
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - ts
    return f"hace {delta.days} dias"


def main() -> int:
    rol = (os.environ.get("ROL") or "").strip().upper()
    rut = (os.environ.get("RUT") or "").strip()
    if not rol and not rut:
        print("Falta ROL= o RUT=", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        q = db.query(Case)
        if rol:
            q = q.filter(Case.rol.ilike(rol))
            buscado = f"ROL {rol}"
        else:
            limpio = rut.replace(".", "").replace("-", "")
            q = q.filter(or_(Case.rol.ilike(f"%{rut}%"), Case.defendant.ilike(f"%{rut}%")))
            buscado = f"RUT {rut} ({limpio})"
        filas = q.all()

        print(f"=== {buscado} ===\n")
        if not filas:
            print("NO ESTA EN LA BASE, bajo ningun abogado.")
            print()
            print("Que significa: nunca la ingerimos. Ninguna mejora de velocidad")
            print("del scraping la va a traer: una causa que no esta no se refresca.")
            print("Esto es el agujero de cobertura, no latencia.")
            return 0

        for c in filas:
            corte = db.get(Court, c.court_id) if c.court_id else None
            ab = db.get(Lawyer, c.lawyer_id) if c.lawyer_id else None
            movs = db.query(Movement).filter(Movement.case_id == c.id).count()
            ultimo = (
                db.query(Movement)
                .filter(Movement.case_id == c.id)
                .order_by(Movement.movement_date.desc())
                .first()
            )
            print(f"case_id {c.id}  estado={c.status}")
            print(f"  tribunal        {corte.name if corte else '(sin tribunal)'}")
            print(f"  abogado         {ab.name if ab else '(sin abogado)'} "
                  f"(id {c.lawyer_id}, activo={getattr(ab, 'is_active', None)}, "
                  f"credencial={bool(getattr(ab, 'encrypted_pjud_password', None) or getattr(ab, 'encrypted_clave_unica_password', None))})")
            print(f"  ultimo detalle  {c.last_detail_checked_at}  ({_dias(c.last_detail_checked_at)})")
            print(f"  ultimo movim.   {c.last_movement_at}  ({movs} movimientos guardados)")
            if ultimo is not None:
                print(f"  el mas reciente {ultimo.movement_date}  {(ultimo.description or '')[:70]}")
            print()
            # El veredicto
            if ab is not None and not ab.is_active:
                print("  >> EL ABOGADO YA NO ESTA ACTIVO: esta causa no se revisa.")
            elif not (getattr(ab, "encrypted_pjud_password", None)
                      or getattr(ab, "encrypted_clave_unica_password", None)):
                print("  >> EL ABOGADO NO TIENE CREDENCIAL DEL PJUD: esta causa no se revisa.")
            elif c.last_detail_checked_at is None:
                print("  >> NUNCA SE LE BAJO EL DETALLE. Esta en la cola de rotacion.")
            else:
                dias = (datetime.now(timezone.utc).replace(tzinfo=None)
                        - c.last_detail_checked_at).days
                if dias > 7:
                    print(f"  >> LATENCIA: no se revisa hace {dias} dias. El movimiento del")
                    print("     cliente puede existir en el PJUD y todavia no estar aca.")
                else:
                    print(f"  >> AL DIA (revisada hace {dias} dias). Si el movimiento no esta,")
                    print("     el PJUD no lo publicaba al momento de la ultima revision,")
                    print("     o el scraper no lo capturo. Merece mirarse en el PJUD.")
            print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
