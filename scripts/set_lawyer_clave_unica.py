#!/usr/bin/env python3
"""CU = Clave Única, the Chilean national identity credential. Stored encrypted only.

Usage: python scripts/set_lawyer_clave_unica.py <pjud_rut> <cu_password> [cu_rut]

Sets the Clave Única credential for a lawyer identified by their PJUD RUT.
Creates the lawyer record if it does not exist yet.  The password is encrypted
with Fernet before storage — the plaintext is never persisted.
"""

import sys
from datetime import datetime

from app.core.database import SessionLocal
from app.core.security import encrypt_pjud_password
from app.models.lawyer import Lawyer
from scripts.cdp_scraper_sketch import normalize_rut


def main() -> None:
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: python scripts/set_lawyer_clave_unica.py <pjud_rut> <cu_password> [cu_rut]")
        sys.exit(1)

    pjud_rut = normalize_rut(sys.argv[1])
    cu_password = sys.argv[2]
    cu_rut = normalize_rut(sys.argv[3]) if len(sys.argv) == 4 else pjud_rut

    db = SessionLocal()
    try:
        lawyer = db.query(Lawyer).filter(Lawyer.rut == pjud_rut).first()
        if lawyer is None:
            lawyer = Lawyer(
                rut=pjud_rut,
                name=pjud_rut,
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(lawyer)
            db.flush()
            print(f"Lawyer created: rut={pjud_rut}")
        else:
            print(f"Lawyer found: {lawyer.name} ({lawyer.rut})")

        lawyer.encrypted_clave_unica_password = encrypt_pjud_password(cu_password)
        lawyer.clave_unica_rut = cu_rut
        db.commit()
        print(f"Clave Única credential stored for {lawyer.name} (pjud_rut={pjud_rut}, cu_rut={cu_rut})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
