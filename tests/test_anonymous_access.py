from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from auth.core import get_current_user, get_optional_user
from database.connection import get_db_for_fastapi
from database.models import Base, Document, ProcessingStatus

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
def anon_client():
    app.dependency_overrides[get_db_for_fastapi] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[get_optional_user] = lambda: None
    with patch("database.connection.Base") as mock_base:
        mock_base.metadata.create_all.return_value = None
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


def test_anonymous_document_workflow(anon_client):
    # 1. Manually add an anonymous document in DB with client IP
    db = TestSessionLocal()
    doc = Document(
        file_name="anon.pdf",
        file_path="/anon.pdf",
        file_size_bytes=100,
        user_id=None,
        ip_address="127.0.0.1",
        status=ProcessingStatus.COMPLETED.value,
    )
    db.add(doc)
    db.commit()
    doc_id = doc.id
    db.close()

    # 2. Try fetching document details anonymously with matching IP
    headers = {"X-Forwarded-For": "127.0.0.1"}
    r = anon_client.get(f"/api/v1/documents/{doc_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["file_name"] == "anon.pdf"

    # 3. Try fetching status with matching IP
    r = anon_client.get(f"/api/v1/documents/{doc_id}/status", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    # 4. Try fetching export CSV with matching IP
    r = anon_client.get(f"/api/v1/documents/{doc_id}/export/csv", headers=headers)
    assert r.status_code == 200

    # 5. Try fetching with mismatched IP
    bad_headers = {"X-Forwarded-For": "192.168.1.1"}
    r = anon_client.get(f"/api/v1/documents/{doc_id}", headers=bad_headers)
    assert r.status_code == 404
