"""One-off migration: re-encrypt ``lawyers.encrypted_pjud_password`` from the
OLD ENCRYPTION_KEY to a NEW real Fernet key.

Run this during the key-rotation maintenance window, BEFORE deploying the strict
ENCRYPTION_KEY validation (which would otherwise reject the old padded key and
leave the existing ciphertext undecryptable).

Sequence for the cutover:
  1. Generate a real Fernet key:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. DRY RUN (changes nothing, just verifies the old key decrypts every row):
       OLD_ENCRYPTION_KEY=<current> NEW_ENCRYPTION_KEY=<new> \
         python scripts/reencrypt_credentials.py
  3. APPLY (re-encrypts in the DB):
       OLD_ENCRYPTION_KEY=<current> NEW_ENCRYPTION_KEY=<new> \
         python scripts/reencrypt_credentials.py --apply
  4. Set ENCRYPTION_KEY=<new> on the VM + local .env*, then deploy the strict code.

Rollback: keep the OLD key; re-run with OLD/NEW swapped to restore.
Secrets come ONLY from env vars — never hardcode or print them.
"""
import base64
import os
import sys

from cryptography.fernet import Fernet


def _build_fernet(key: str, *, strict: bool) -> Fernet:
    try:
        return Fernet(key.encode())
    except Exception:
        if strict:
            raise
        return Fernet(base64.urlsafe_b64encode(key.encode().ljust(32)[:32]))


def main() -> int:
    apply = "--apply" in sys.argv
    old = os.environ.get("OLD_ENCRYPTION_KEY")
    new = os.environ.get("NEW_ENCRYPTION_KEY")
    if not old or not new:
        print("ERROR: set OLD_ENCRYPTION_KEY and NEW_ENCRYPTION_KEY env vars.")
        return 2

    old_f = _build_fernet(old, strict=False)  # current key may be padded
    try:
        new_f = _build_fernet(new, strict=True)  # NEW must be a real Fernet key
    except Exception as exc:
        print(f"ERROR: NEW_ENCRYPTION_KEY is not a real Fernet key: {exc}")
        return 2

    from app.core.database import SessionLocal
    from app.models.lawyer import Lawyer

    db = SessionLocal()
    rows = db.query(Lawyer).filter(Lawyer.encrypted_pjud_password.isnot(None)).all()
    print(f"rows with encrypted_pjud_password: {len(rows)}")

    done = 0
    for law in rows:
        ct = str(law.encrypted_pjud_password).encode()
        try:
            plain = old_f.decrypt(ct)
        except Exception as exc:
            print(f"  lawyer {law.id}: OLD key FAILED to decrypt -> {type(exc).__name__}. Aborting.")
            return 1
        new_ct = new_f.encrypt(plain).decode()
        if apply:
            law.encrypted_pjud_password = new_ct
        done += 1
        print(f"  lawyer {law.id}: decrypt OK ({len(plain)} bytes) -> re-encrypted")

    if apply:
        db.commit()
        print(f"APPLIED: {done} row(s) re-encrypted with the new key.")
    else:
        print(f"DRY RUN ok: {done} row(s) would be re-encrypted. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
