"""One-off migration: re-encrypt the lawyers' encrypted credential columns from
the OLD ENCRYPTION_KEY to a NEW real Fernet key.

Robust by design (does NOT import app.config, so strict ENCRYPTION_KEY validation
can never block it): it builds its own engine from DATABASE_URL and uses raw SQL.
Migrates BOTH encrypted columns. The whole migration runs in ONE transaction
(atomic): with --apply, either every row is re-encrypted or nothing changes.

Run during the key-rotation window, BEFORE deploying strict validation:
  1. New Fernet key:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. DRY RUN (verifies the old key decrypts every row, changes nothing):
       OLD_ENCRYPTION_KEY=<current> NEW_ENCRYPTION_KEY=<new> DATABASE_URL=<url> \
         python scripts/reencrypt_credentials.py
  3. APPLY:
       ... python scripts/reencrypt_credentials.py --apply
  4. Set ENCRYPTION_KEY=<new> on the VM + local, then deploy strict.

Rollback: re-run with OLD/NEW swapped. Secrets come ONLY from env vars.
"""
import base64
import os
import sys

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

# Both reversible-credential columns on the lawyers table.
ENCRYPTED_COLUMNS = ("encrypted_pjud_password", "encrypted_clave_unica_password")


class _DryRun(Exception):
    """Sentinel to roll back the transaction after a dry run."""


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
    db_url = os.environ.get("DATABASE_URL")
    if not old or not new or not db_url:
        print("ERROR: set OLD_ENCRYPTION_KEY, NEW_ENCRYPTION_KEY and DATABASE_URL.")
        return 2

    old_f = _build_fernet(old, strict=False)  # current key may be padded
    try:
        new_f = _build_fernet(new, strict=True)  # NEW must be a real Fernet key
    except Exception as exc:
        print(f"ERROR: NEW_ENCRYPTION_KEY is not a real Fernet key: {exc}")
        return 2

    engine = create_engine(db_url)
    total = 0
    try:
        # One transaction for the whole migration → atomic with --apply.
        with engine.begin() as conn:
            for col in ENCRYPTED_COLUMNS:
                rows = conn.execute(
                    text(f"SELECT id, {col} FROM lawyers WHERE {col} IS NOT NULL")
                ).fetchall()
                print(f"{col}: {len(rows)} row(s) populated")
                for rid, ct in rows:
                    plain = old_f.decrypt(str(ct).encode())  # verifies old key
                    new_ct = new_f.encrypt(plain).decode()
                    if apply:
                        conn.execute(
                            text(f"UPDATE lawyers SET {col} = :v WHERE id = :i"),
                            {"v": new_ct, "i": rid},
                        )
                    total += 1
                    print(f"  lawyer {rid} [{col}]: decrypt OK ({len(plain)} bytes) -> re-encrypted")
            if not apply:
                raise _DryRun()  # roll back the no-op transaction
    except _DryRun:
        print(f"DRY RUN ok: {total} value(s) would be re-encrypted. Re-run with --apply.")
        return 0
    except Exception as exc:
        print(f"ABORTED (nothing changed): {type(exc).__name__}: {str(exc)[:120]}")
        return 1

    print(f"APPLIED: {total} value(s) re-encrypted with the new key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
