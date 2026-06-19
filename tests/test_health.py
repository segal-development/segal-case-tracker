"""Tests for the liveness (/health) and readiness (/readyz) probes."""

from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.main import app


def test_health_is_liveness_only(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_readyz_ok_when_db_reachable(client: TestClient):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True, "db": "ok"}


def test_readyz_503_when_db_unreachable(client: TestClient):
    """If the DB query raises, /readyz must return 503 (so deploys fail fast)."""

    class _BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    def _broken_db():
        yield _BrokenSession()

    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _broken_db
    try:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert "db unavailable" in resp.json()["detail"]
    finally:
        if prev is not None:
            app.dependency_overrides[get_db] = prev
        else:
            app.dependency_overrides.pop(get_db, None)
