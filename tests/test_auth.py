"""
tests/test_auth.py
───────────────────
Basic tests for auth endpoints.

Why write tests?
  - CI/CD runs these on every push
  - If register breaks, deploy is blocked before it reaches Render
  - Catches regressions — old bugs coming back after changes

Run locally: pytest tests/ -v
"""

import pytest
from fastapi.testclient import TestClient


# ── Helpers ──────────────────────────────────────────────────

def get_client():
    """Import app only when needed — avoids DB connection at import time."""
    from api.main import app
    return TestClient(app)


# ── Auth tests ───────────────────────────────────────────────

def test_register_success():
    client = get_client()
    response = client.post("/auth/register", json={
        "email": "test_ci@example.com",
        "password": "testpassword123",
        "full_name": "CI Test User",
    })
    assert response.status_code in (201, 409)   # 409 if already exists from previous run


def test_register_weak_password():
    client = get_client()
    response = client.post("/auth/register", json={
        "email": "weak@example.com",
        "password": "123",   # too short
    })
    assert response.status_code == 422


def test_register_invalid_email():
    client = get_client()
    response = client.post("/auth/register", json={
        "email": "not-an-email",
        "password": "validpassword123",
    })
    assert response.status_code == 422


def test_login_wrong_password():
    client = get_client()
    response = client.post("/auth/login", json={
        "email": "test_ci@example.com",
        "password": "wrongpassword",
    })
    assert response.status_code == 401


def test_protected_route_no_token():
    client = get_client()
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_health_endpoint():
    client = get_client()
    response = client.get("/health")
    assert response.status_code == 200


def test_stats_endpoint():
    client = get_client()
    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_documents" in data