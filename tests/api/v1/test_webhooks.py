"""Tests for webhook CRUD endpoints — Strict TDD (written before implementation)."""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.lawyer import Lawyer
from app.models.webhook import Webhook


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    """Create a primary test lawyer in the DB."""
    obj = Lawyer(rut="11111111-1", name="Test Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    """TestClient with get_current_lawyer stubbed to the primary test lawyer."""

    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client
    # app.dependency_overrides is cleared by the parent `client` fixture teardown


# ---------------------------------------------------------------------------
# POST /webhooks
# ---------------------------------------------------------------------------


class TestCreateWebhook:
    def test_create_webhook_auto_generates_secret(self, authed_client: TestClient):
        """POST without secret → 201, non-empty auto-generated secret, is_active True."""
        response = authed_client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["secret"], "secret must be non-empty"
        assert data["is_active"] is True
        assert "example.com/hook" in data["url"]

    def test_create_webhook_with_explicit_secret(self, authed_client: TestClient):
        """POST with explicit secret → that exact secret is stored and returned."""
        my_secret = "my-super-secret-token"
        response = authed_client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook", "secret": my_secret},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["secret"] == my_secret

    def test_create_webhook_sets_lawyer_id(self, authed_client: TestClient, db, lawyer):
        """POST → persisted webhook has the correct lawyer_id."""
        response = authed_client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/hook"},
        )
        assert response.status_code == 201
        webhook_id = response.json()["id"]

        persisted = db.query(Webhook).filter(Webhook.id == webhook_id).first()
        assert persisted is not None
        assert persisted.lawyer_id == lawyer.id


# ---------------------------------------------------------------------------
# GET /webhooks
# ---------------------------------------------------------------------------


class TestListWebhooks:
    def test_list_returns_own_webhooks_only(self, authed_client: TestClient, db, lawyer):
        """GET /webhooks → returns only webhooks for the current lawyer."""
        # Create a second lawyer and a webhook for them directly in the DB.
        other = Lawyer(rut="22222222-2", name="Other Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)

        other_hook = Webhook(
            lawyer_id=other.id,
            url="https://other.example.com/hook",
            events=["movement.created"],
            is_active=True,
        )
        db.add(other_hook)
        db.commit()

        # Create one webhook for our lawyer via the API.
        authed_client.post(
            "/api/v1/webhooks",
            json={"url": "https://mine.example.com/hook"},
        )

        response = authed_client.get("/api/v1/webhooks")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        assert "mine.example.com" in items[0]["url"]


# ---------------------------------------------------------------------------
# GET /webhooks/{id}
# ---------------------------------------------------------------------------


class TestGetWebhook:
    def test_get_nonexistent_webhook_returns_404(self, authed_client: TestClient):
        """GET /webhooks/99999 → 404."""
        response = authed_client.get("/api/v1/webhooks/99999")
        assert response.status_code == 404

    def test_get_other_lawyers_webhook_returns_404(
        self, authed_client: TestClient, db
    ):
        """GET /webhooks/{id} for a webhook owned by a different lawyer → 404."""
        other = Lawyer(rut="33333333-3", name="Third Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)

        hook = Webhook(
            lawyer_id=other.id,
            url="https://other.example.com/hook",
            events=["movement.created"],
            is_active=True,
        )
        db.add(hook)
        db.commit()
        db.refresh(hook)

        response = authed_client.get(f"/api/v1/webhooks/{hook.id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /webhooks/{id}
# ---------------------------------------------------------------------------


class TestDeleteWebhook:
    def test_delete_removes_webhook(self, authed_client: TestClient):
        """DELETE /webhooks/{id} → 204; subsequent GET → 404."""
        create_resp = authed_client.post(
            "/api/v1/webhooks",
            json={"url": "https://example.com/to-delete"},
        )
        assert create_resp.status_code == 201
        webhook_id = create_resp.json()["id"]

        del_resp = authed_client.delete(f"/api/v1/webhooks/{webhook_id}")
        assert del_resp.status_code == 204

        get_resp = authed_client.get(f"/api/v1/webhooks/{webhook_id}")
        assert get_resp.status_code == 404
