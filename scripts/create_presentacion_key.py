"""Generate an API key for the external Presentación (GEDOC escrito-upload) API.

The plaintext key is printed ONCE at generation time and is never stored — only
its SHA-256 hash is persisted (see ``app/models/presentacion_api_key.py`` and
``app/api/presentacion/deps.require_presentacion_key``). Hand it to the GEDOC
integration immediately; it cannot be recovered afterwards.

Standalone: own engine from ``DATABASE_URL`` (like scripts/create_redaccion_key.py),
so it needs no running app.

Usage:
  DATABASE_URL=postgresql://... python scripts/create_presentacion_key.py --label gedoc
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
        "--label", required=True, help="Consumer label, e.g. gedoc"
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        return 2

    from app.models.presentacion_api_key import PresentacionApiKey

    engine = create_engine(db_url)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        plaintext = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        key = PresentacionApiKey(
            label=args.label,
            key_hash=key_hash,
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.add(key)
        db.commit()
        db.refresh(key)

        print(f"Generated Presentación API key for '{args.label}' (id={key.id}).")
        print("Save this NOW — it will NOT be shown again:\n")
        print(f"  {plaintext}\n")
        print("Send it as the 'X-API-Key' header to /api/presentacion/v1/presentaciones.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
