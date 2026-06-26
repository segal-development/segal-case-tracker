"""Load PJUD 2ª claves from a LOCAL file, encrypt (Fernet) and store per lawyer.

SECURITY — read this:
  - NEVER paste claves in chat. Put them in a local file, one per line:
        16021492-9=LaClaveDeEseAbogado
        19456852-5=OtraClave
    (lines starting with # are ignored)
  - The file MUST be outside git and chmod 600:
        touch /tmp/claves_pjud.txt && chmod 600 /tmp/claves_pjud.txt
        # edit it, then run this script, then DELETE it:
        shred -u /tmp/claves_pjud.txt   (or: rm -P)
  - The script reads the file, encrypts each clave, stores it in
    lawyers.encrypted_pjud_password, and NEVER logs the plaintext.

Run:
  CLAVES_FILE=/tmp/claves_pjud.txt PYTHONPATH=. <venv> python scripts/load_pjud_passwords.py
"""
import os

CLAVES_FILE = os.environ.get("CLAVES_FILE", "")


def main() -> int:
    if not CLAVES_FILE or not os.path.exists(CLAVES_FILE):
        print("Set CLAVES_FILE to a local file with 'rut=clave' lines (chmod 600, outside git).")
        return 2

    from app.core.database import SessionLocal
    from app.core.security import encrypt_pjud_password
    from app.models.lawyer import Lawyer

    db = SessionLocal()
    loaded = skipped = 0
    with open(CLAVES_FILE) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            rut, clave = line.split("=", 1)
            rut, clave = rut.strip(), clave.strip()
            lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
            if not lawyer:
                print(f"  ⚠ {rut}: lawyer not found — skipped")
                skipped += 1
                continue
            lawyer.encrypted_pjud_password = encrypt_pjud_password(clave)
            loaded += 1
            print(f"  ✓ {rut}: 2ª clave encrypted + stored")  # never logs the clave
    db.commit()
    print(f"\n{loaded} clave(s) loaded, {skipped} skipped.")
    print(f"⚠ DELETE the file now:  shred -u {CLAVES_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
