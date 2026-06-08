"""Tests for scrapper endpoints."""

import pytest
from fastapi.testclient import TestClient


def test_search_requires_auth(client: TestClient):
    """Search endpoint should require authentication."""
    response = client.get("/api/v1/scrapper/search?rol=C-1234-2024")
    assert response.status_code == 401


def test_search_requires_params(client: TestClient, auth_headers: dict):
    """Search should require rol or rut parameter."""
    # TODO: Mock authentication
    # response = client.get("/api/v1/scrapper/search", headers=auth_headers)
    # assert response.status_code == 400
    pass


def test_poll_job(client: TestClient, auth_headers: dict):
    """Polling a job should return status."""
    # TODO: Mock authentication
    # response = client.get(
    #     "/api/v1/scrapper/poll/job-12345",
    #     headers=auth_headers,
    # )
    # assert response.status_code == 200
    # data = response.json()
    # assert "status" in data
    pass
