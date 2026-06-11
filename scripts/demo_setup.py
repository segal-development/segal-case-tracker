"""Demo setup helper — seeds a Lawyer and mints a usable JWT for local/demo use.

Usage:
    python scripts/demo_setup.py --email you@segal.cl --rut 16021492-9

The env-var guard MUST run before any app.* import so pydantic-settings
does not blow up with the production-secrets validator.
"""

import os

os.environ.setdefault("ENVIRONMENT", "development")

import argparse  # noqa: E402
from datetime import timedelta  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.core.security import create_access_token, decode_access_token  # noqa: E402  # noqa: F401
from app.models.lawyer import Lawyer  # noqa: E402


# ---------------------------------------------------------------------------
# Core testable functions
# ---------------------------------------------------------------------------


def ensure_demo_lawyer(
    db,
    rut: str,
    email: str,
    name: str = "Demo Lawyer",
) -> Lawyer:
    """Look up a Lawyer by rut; create one if absent, update email if changed.

    Idempotent — calling multiple times with the same rut is safe.
    Returns the persisted Lawyer instance with a valid integer id.
    """
    lawyer = db.query(Lawyer).filter(Lawyer.rut == rut).first()

    if lawyer is None:
        lawyer = Lawyer(
            rut=rut,
            name=name,
            email=email,
            is_active=True,
        )
        db.add(lawyer)
    else:
        lawyer.email = email

    db.commit()
    db.refresh(lawyer)
    return lawyer


def mint_token(lawyer_id: int, hours: int = 24) -> str:
    """Return a JWT whose ``sub`` claim is ``str(lawyer_id)``.

    This token is accepted by /sync and /webhooks because those endpoints
    resolve the lawyer via ``int(current_lawyer["sub"])``.
    """
    return create_access_token(
        data={"sub": str(lawyer_id)},
        expires_delta=timedelta(hours=hours),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a demo Lawyer and print a usable JWT for local/demo auth.",
    )
    parser.add_argument("--email", required=True, help="Lawyer email address")
    parser.add_argument(
        "--rut",
        default="16021492-9",
        help="Lawyer RUT (default: 16021492-9)",
    )
    parser.add_argument(
        "--name",
        default="Demo Lawyer",
        help="Lawyer display name (default: Demo Lawyer)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Token validity in hours (default: 24)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        lawyer = ensure_demo_lawyer(db, rut=args.rut, email=args.email, name=args.name)
        token = mint_token(lawyer.id, hours=args.hours)
    finally:
        db.close()

    print(f"Lawyer id: {lawyer.id}")
    print(f"RUT: {lawyer.rut}")
    print(f"Email: {lawyer.email}")
    print("JWT (use as: Authorization: Bearer <token>):")
    print(token)


if __name__ == "__main__":
    main()
