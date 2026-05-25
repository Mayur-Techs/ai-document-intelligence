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

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.connection import get_db_for_fastapi
from database.models import Base

TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    from api.main import app

    app.dependency_overrides[get_db_for_fastapi] = override_get_db
    with patch("database.connection.Base") as mock_base:
        mock_base.metadata.create_all.return_value = None
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


# ── Auth tests ───────────────────────────────────────────────


def test_register_success(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "test_ci@example.com",
            "password": "testpassword123",
            "full_name": "CI Test User",
        },
    )
    assert response.status_code in (201, 409)


def test_register_weak_password(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "weak@example.com",
            "password": "123",  # too short
        },
    )
    assert response.status_code == 422


def test_register_invalid_email(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "not-an-email",
            "password": "validpassword123",
        },
    )
    assert response.status_code == 422


def test_login_wrong_password(client):
    # Register the user first so they exist
    client.post(
        "/auth/register",
        json={
            "email": "test_ci@example.com",
            "password": "testpassword123",
            "full_name": "CI Test User",
        },
    )
    response = client.post(
        "/auth/login",
        json={
            "email": "test_ci@example.com",
            "password": "wrongpassword",
        },
    )
    assert response.status_code == 401


def test_protected_route_no_token(client):
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200


def test_stats_endpoint(client):
    response = client.get("/api/v1/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_documents" in data
