#!/usr/bin/env python3
"""CLI script to set a lawyer's app-level password hash.

Usage: python scripts/set_lawyer_password.py <email> <password>

For initial setup only.  Requires a lawyer record with the given email to
already exist in the database.  The password is bcrypt-hashed before storage —
the plaintext is never persisted.
"""

import sys

from sqlalchemy import func

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.lawyer import Lawyer


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/set_lawyer_password.py <email> <password>")
        sys.exit(1)

    email, password = sys.argv[1], sys.argv[2]
    if len(password) < 8:
        print("Error: password must be at least 8 characters")
        sys.exit(1)

    db = SessionLocal()
    try:
        lawyer = db.query(Lawyer).filter(func.lower(Lawyer.email) == email.lower()).first()
        if not lawyer:
            print(f"Error: no lawyer found with email {email!r}")
            sys.exit(1)
        lawyer.password_hash = hash_password(password)
        db.commit()
        print(f"Password set for {lawyer.name} ({lawyer.email})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
