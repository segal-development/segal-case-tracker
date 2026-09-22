"""One-off/CI entrypoint to (re-)seed the matriz reference tables.

Idempotent — safe to re-run whenever ``app/data/matriz/*.csv`` changes
(e.g. after ``scripts/matriz_xlsx_to_csv.py`` regenerates them from an
updated business xlsx).

Run:
    PYTHONPATH=$(pwd) poetry run python scripts/seed_matriz.py
"""
import os

os.environ.setdefault("ENVIRONMENT", "production")


def main() -> None:
    from app.core.database import SessionLocal
    from app.services.matriz_seed import seed_matriz

    db = SessionLocal()
    try:
        result = seed_matriz(db)
        for table, counts in result.items():
            print(f"{table}: {counts}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
