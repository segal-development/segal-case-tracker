"""Generate, rotate, or revoke an operator ingest API key.

The plaintext key is printed ONCE at generation/rotation time and is never
stored — only its SHA-256 hash is persisted (see ``app/models/ingest_key.py``
and ``app/api/deps.require_ingest_key``). Copy it into the browser
extension's settings immediately; it cannot be recovered afterwards.

Usage:
  PYTHONPATH=. <venv> python scripts/manage_ingest_key.py generate --label operator-carla
  PYTHONPATH=. <venv> python scripts/manage_ingest_key.py rotate --label operator-carla
  PYTHONPATH=. <venv> python scripts/manage_ingest_key.py revoke --label operator-carla
"""
import argparse
import hashlib
import secrets
import sys
from datetime import datetime


def _generate(db, label: str) -> int:
    from app.models.ingest_key import IngestKey

    plaintext = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    key = IngestKey(
        label=label,
        key_hash=key_hash,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    print(f"Generated ingest key for '{label}' (id={key.id}).")
    print("Copy this NOW — it will not be shown again:\n")
    print(f"  {plaintext}\n")
    return 0


def _rotate(db, label: str) -> int:
    from app.models.ingest_key import IngestKey

    active = (
        db.query(IngestKey)
        .filter(IngestKey.label == label, IngestKey.is_active.is_(True))
        .all()
    )
    now = datetime.utcnow()
    for old_key in active:
        old_key.is_active = False
        old_key.revoked_at = now
    db.commit()
    print(f"Revoked {len(active)} previous active key(s) for '{label}'.")
    return _generate(db, label)


def _revoke(db, label: str) -> int:
    from app.models.ingest_key import IngestKey

    active = (
        db.query(IngestKey)
        .filter(IngestKey.label == label, IngestKey.is_active.is_(True))
        .all()
    )
    if not active:
        print(f"No active key found for '{label}'.")
        return 1
    now = datetime.utcnow()
    for key in active:
        key.is_active = False
        key.revoked_at = now
    db.commit()
    print(f"Revoked {len(active)} key(s) for '{label}'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["generate", "rotate", "revoke"])
    parser.add_argument("--label", required=True, help="Operator label, e.g. operator-carla")
    args = parser.parse_args()

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        if args.action == "generate":
            return _generate(db, args.label)
        if args.action == "rotate":
            return _rotate(db, args.label)
        return _revoke(db, args.label)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
