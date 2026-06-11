"""Pytest configuration and fixtures."""

import os
os.environ.setdefault("ENVIRONMENT", "development")

import pytest
import fakeredis.aioredis
from datetime import timedelta, timezone, datetime
from typing import Generator
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.api.deps import get_db
from app.core.database import Base
from app.core.security import create_access_token


# Use SQLite for tests
SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db() -> Generator:
    """Create test database and yield session."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db) -> Generator:
    """Create test client with overridden dependencies."""
    
    def override_get_db():
        try:
            yield db
        finally:
            pass
    
    app.dependency_overrides[get_db] = override_get_db
    
    with TestClient(app) as c:
        yield c
    
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers() -> dict:
    """Return auth headers with a valid JWT for the test RUT."""
    token = create_access_token({"sub": "11111111-1"}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# S1-T0: Async fixtures for session-store and scraper tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    """In-memory async Redis backed by fakeredis (no real connection needed)."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def session_store(fake_redis):
    """SessionStore with injected fake async Redis client."""
    from app.services.session_store import SessionStore
    return SessionStore(redis_client=fake_redis)


@pytest.fixture
def sample_session():
    """A fresh, non-expired PJUDSession for use in tests."""
    from app.services.pjud_session import PJUDSession
    return PJUDSession.create(
        rut="12345678-9",
        cookies=[{"name": "PHPSESSID", "value": "testcookie", "domain": ".pjud.cl"}],
        lawyer_id=42,
        auth_method="captcha",
    )


@pytest.fixture
def mock_scraper():
    """Lightweight scraper mock — avoids Playwright startup in unit tests."""
    from unittest.mock import AsyncMock, MagicMock
    from app.services.pjud_session import PJUDSession
    scraper = MagicMock()
    scraper.login_with_token = AsyncMock(
        return_value=PJUDSession.create(
            rut="12345678-9",
            cookies=[{"name": "PHPSESSID", "value": "abc", "domain": ".pjud.cl"}],
            lawyer_id=0,
            auth_method="captcha",
        )
    )
    scraper.start = AsyncMock()
    scraper.stop = AsyncMock()
    scraper.close = AsyncMock()
    return scraper
