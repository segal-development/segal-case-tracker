"""Tests for GET /api/v1/goals and PUT /api/v1/goals/{key} endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer


ACCOUNT_RUT = "11111111-1"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Goals Test Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


# ---------------------------------------------------------------------------
# GET /api/v1/goals
# ---------------------------------------------------------------------------


def test_get_goals_empty(authed_client):
    r = authed_client.get("/api/v1/goals")
    assert r.status_code == 200
    assert r.json() == {}


# ---------------------------------------------------------------------------
# PUT /api/v1/goals/{key}
# ---------------------------------------------------------------------------


def test_put_goal_creates_and_reads(authed_client):
    # Create via PUT
    r = authed_client.put(
        "/api/v1/goals/monthly_productivity",
        json={"value": 120},
    )
    assert r.status_code == 200
    assert r.json() == {"key": "monthly_productivity", "value": 120}

    # GET should reflect the new goal
    r = authed_client.get("/api/v1/goals")
    assert r.status_code == 200
    assert r.json() == {"monthly_productivity": 120}


def test_put_goal_updates(authed_client, db):
    from app.models.goal import Goal

    # Insert initial value
    authed_client.put("/api/v1/goals/monthly_productivity", json={"value": 120})

    # Update to a new value
    r = authed_client.put(
        "/api/v1/goals/monthly_productivity",
        json={"value": 150},
    )
    assert r.status_code == 200
    assert r.json() == {"key": "monthly_productivity", "value": 150}

    # GET reflects the updated value
    r = authed_client.get("/api/v1/goals")
    assert r.json() == {"monthly_productivity": 150}

    # Only one row in db (upsert, not insert)
    count = db.query(Goal).filter(Goal.key == "monthly_productivity").count()
    assert count == 1


def test_put_goal_negative_value(authed_client):
    r = authed_client.put(
        "/api/v1/goals/monthly_productivity",
        json={"value": -1},
    )
    assert r.status_code == 422
