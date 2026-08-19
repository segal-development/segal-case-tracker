"""Database connection and session management."""

from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker, declarative_base

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    # Detect and replace dead connections at checkout (Cloud SQL / the auth proxy
    # silently close idle connections; without this, the first query on a stale
    # connection raises "server closed the connection unexpectedly").
    pool_pre_ping=True,
    # Proactively retire connections older than 30 min — comfortably under Cloud
    # SQL's idle cutoff — so long-running workers never hand out a stale one.
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
