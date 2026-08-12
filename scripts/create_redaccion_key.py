"""Generate an API key for the external read-only Redaccion API.

The plaintext key is printed ONCE at generation time and is never stored — only
its SHA-256 hash is persisted (see ``app/models/redaccion_api_key.py`` and
``app/api/redaccion/deps.require_redaccion_key``). Hand it to the Redaccion
integration immediately; it cannot be recovered afterwards.

Standalone: own engine from ``DATABASE_URL`` (like scripts/create_sysgal_key.py),
so it needs no running app.

Usage:
  DATABASE_URL=postgresql://... python scripts/create_redaccion_key.py --label redaccion
"""
import argparse
import hashlib
import os
import secrets
import sys
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", required=True, help="Consumer label, e.g. redaccion"
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 2

    from app.models.redaccion_api_key import RedaccionApiKey

    engine = create_engine(db_url)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        plaintext = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        key = RedaccionApiKey(
            label=args.label,
            key_hash=key_hash,
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.add(key)
        db.commit()
        db.refresh(key)

        print(f"Generated Redaccion API key for '{args.label}' (id={key.id}).")
        print("Save this NOW — it will NOT be shown again:\n")
        print(f"  {plaintext}\n")
        print("Send it as the 'X-API-Key' header to /api/redaccion/v1/buscar.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
