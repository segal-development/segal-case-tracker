"""Interactively load PJUD 2ª claves for lawyers — NO file, NO plaintext on disk.

Prompts for the RUT (visible) and the 2ª clave (HIDDEN — not echoed to the
screen, via getpass), encrypts it (Fernet) and stores it in
``lawyers.encrypted_pjud_password``. It NEVER prints the clave and never writes
it to disk. Loop for as many lawyers as you want; empty RUT ends the session.

Run it in a REAL terminal (macOS Terminal app), where hidden input works:
  bash scripts/cargar_clave_interactiva.sh
"""
import getpass


def main() -> int:
    from app.core.database import SessionLocal
    from app.core.security import encrypt_pjud_password
    from app.models.lawyer import Lawyer
    from app.utils.rut import clean_rut

    db = SessionLocal()
    loaded = 0
    print(
        "\nCargar 2ª claves PJUD — la clave NO se muestra mientras la tipeás.\n"
        "Dejá el RUT vacío (solo Enter) para terminar.\n"
    )
    try:
        while True:
            try:
                rut = input("RUT del abogado (ej. 19586894-8), o Enter para terminar: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not rut:
                break
            # Normalize the typed RUT the same way it's stored (dots/spaces removed,
            # verification digit upper-cased) so "20217325-k" and "20.217.325-K" both
            # match the canonical stored value.
            rut = clean_rut(rut)
            lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()
            if not lawyer:
                print(f"  ⚠ {rut}: no hay abogado con ese RUT — salteado\n")
                continue
            clave = getpass.getpass(f"  2ª clave de {lawyer.name} (no se muestra): ").strip()
            if not clave:
                print("  (vacío — salteado)\n")
                continue
            confirm = getpass.getpass("  Repetí la clave para confirmar: ").strip()
            if clave != confirm:
                print("  ✗ las claves no coinciden — salteado\n")
                clave = confirm = ""
                continue
            lawyer.encrypted_pjud_password = encrypt_pjud_password(clave)
            db.commit()
            clave = confirm = ""  # drop plaintext from memory right away
            loaded += 1
            print(f"  ✓ {rut} · {lawyer.name}: 2ª clave encriptada y guardada\n")
    finally:
        db.close()

    print(f"Listo: {loaded} clave(s) cargada(s). Nada quedó en texto plano en disco.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
