"""Tests for cases endpoints."""

import pytest
from fastapi.testclient import TestClient


def test_list_cases_requires_auth(client: TestClient):
    """Cases endpoint should require authentication."""
    response = client.get("/api/v1/cases")
    assert response.status_code == 401


def test_list_cases_empty(client: TestClient, auth_headers: dict):
    """List cases should return empty list initially."""
    # TODO: Mock authentication
    # response = client.get("/api/v1/cases", headers=auth_headers)
    # assert response.status_code == 200
    # data = response.json()
    # assert data["items"] == []
    # assert data["total"] == 0
    pass


def test_create_case(client: TestClient, auth_headers: dict):
    """Creating a case should return case data."""
    # TODO: Mock authentication
    # response = client.post(
    #     "/api/v1/cases",
    #     headers=auth_headers,
    #     json={
    #         "rol": "C-1234-2024",
    #         "court_id": 1,
    #     },
    # )
    # assert response.status_code == 201
    # data = response.json()
    # assert data["rol"] == "C-1234-2024"
    pass


def test_get_nonexistent_case(client: TestClient, auth_headers: dict):
    """Getting a non-existent case should return 404."""
    # TODO: Mock authentication
    # response = client.get("/api/v1/cases/99999", headers=auth_headers)
    # assert response.status_code == 404
    pass
