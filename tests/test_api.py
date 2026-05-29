"""
tests/test_api.py — Integration tests for all API routes.

Same test infrastructure pattern as System 1:
  - StaticPool: all SQLite connections share one in-memory database
  - patch("database.connection.Base"): makes init_db a no-op
  - dependency_overrides: swaps PostgreSQL session for SQLite
  - autouse fixture: creates/drops tables per test

Run: pytest tests/test_api.py -v  (no Docker required)
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth.core import get_current_user, get_optional_user
from database.connection import get_db_for_fastapi
from database.models import Base, Document, ProcessingStatus, User, UserPlan

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

    dummy_user = User(
        id=1,
        email="test_api@example.com",
        full_name="API Test User",
        plan=UserPlan.free,
        files_used_today=0,
        files_used_month=0,
        is_active=True,
    )

    app.dependency_overrides[get_db_for_fastapi] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: dummy_user
    app.dependency_overrides[get_optional_user] = lambda: dummy_user
    with patch("database.connection.Base") as mock_base:
        mock_base.metadata.create_all.return_value = None
        with TestClient(app) as c:
            yield c
    app.dependency_overrides.clear()


def _make_pdf_upload(content: bytes = b"%PDF-1.4 test", filename: str = "invoice.pdf"):
    return {"file": (filename, io.BytesIO(content), "application/pdf")}


# ── Health ────────────────────────────────────────────────────────────────────
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["service"] == "doc-intelligence"


# ── Upload ────────────────────────────────────────────────────────────────────
class TestUpload:
    def test_upload_pdf_returns_202(self, client):
        with patch("api.routes.documents.validate_file") as mock_val, patch(
            "api.routes.documents.save_upload"
        ) as mock_save, patch("api.routes.documents.process_document"):
            mock_val.return_value = type(
                "V",
                (),
                {
                    "valid": True,
                    "error": None,
                    "original_name": "invoice.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 1024,
                },
            )()
            mock_save.return_value = type(
                "S",
                (),
                {
                    "path": "/data/raw/abc.pdf",
                    "size_bytes": 1024,
                },
            )()

            r = client.post(
                "/api/v1/documents/upload",
                files=_make_pdf_upload(),
            )

        assert r.status_code == 202
        data = r.json()
        assert "document_id" in data
        assert data["status"] == "queued"
        assert "Poll" in data["message"]

    def test_upload_non_pdf_returns_422(self, client):
        with patch("api.routes.documents.validate_file") as mock_val:
            mock_val.return_value = type(
                "V",
                (),
                {
                    "valid": False,
                    "error": "Only PDF files are accepted. Got: image/jpeg",
                },
            )()

            r = client.post(
                "/api/v1/documents/upload",
                files={"file": ("photo.jpg", io.BytesIO(b"fake jpg"), "image/jpeg")},
            )
        assert r.status_code == 422

    def test_batch_upload_enforces_rate_limit_per_valid_file(self, client):
        with patch("api.routes.documents.validate_file") as mock_val, patch(
            "api.routes.documents.save_upload"
        ) as mock_save, patch("api.routes.documents.process_document"), patch(
            "api.routes.documents.enforce_rate_limit"
        ) as mock_limit:
            mock_val.return_value = type(
                "V",
                (),
                {
                    "valid": True,
                    "error": None,
                    "original_name": "invoice.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 1024,
                },
            )()
            mock_save.return_value = type(
                "S",
                (),
                {
                    "path": "/data/raw/abc.pdf",
                    "size_bytes": 1024,
                },
            )()
            mock_limit.return_value = "testclient"

            r = client.post(
                "/api/v1/documents/batch/upload",
                files=[
                    ("files", ("a.pdf", io.BytesIO(b"%PDF-1.4 a"), "application/pdf")),
                    ("files", ("b.pdf", io.BytesIO(b"%PDF-1.4 b"), "application/pdf")),
                ],
            )

        assert r.status_code == 202
        assert r.json()["queued"] == 2
        assert mock_limit.call_count == 2


# ── Status polling ────────────────────────────────────────────────────────────
class TestDocumentStatus:
    def _create_doc(self, db) -> Document:
        doc = Document(
            file_name="test_invoice.pdf",
            file_path="/data/raw/test.pdf",
            file_size_bytes=12345,
            user_id=1,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc

    def test_status_queued_after_upload(self, client):
        db = TestSessionLocal()
        doc = self._create_doc(db)
        db.close()

        r = client.get(f"/api/v1/documents/{doc.id}/status")
        assert r.status_code == 200
        assert r.json()["status"] == "queued"

    def test_status_not_found_404(self, client):
        assert client.get("/api/v1/documents/99999/status").status_code == 404

    def test_get_full_document(self, client):
        db = TestSessionLocal()
        doc = self._create_doc(db)
        db.close()

        r = client.get(f"/api/v1/documents/{doc.id}")
        assert r.status_code == 200
        d = r.json()
        assert d["file_name"] == "test_invoice.pdf"
        assert d["status"] == "queued"
        assert "fields" in d

    def test_list_documents(self, client):
        db = TestSessionLocal()
        self._create_doc(db)
        self._create_doc(db)
        db.close()

        r = client.get("/api/v1/documents/")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_filter_by_status(self, client):
        db = TestSessionLocal()
        doc = self._create_doc(db)
        # Manually set one to completed
        doc.status = ProcessingStatus.COMPLETED.value
        db.commit()
        self._create_doc(db)  # stays queued
        db.close()

        r = client.get("/api/v1/documents/?status=completed")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["status"] == "completed"


# ── Stats ─────────────────────────────────────────────────────────────────────
class TestStats:
    def test_stats_empty_db(self, client):
        r = client.get("/api/v1/documents/stats/summary")
        assert r.status_code == 200
        assert r.json()["total_documents"] == 0

    def test_stats_with_documents(self, client):
        db = TestSessionLocal()
        db.add(Document(file_name="a.pdf", file_path="/a", file_size_bytes=100, user_id=1))
        db.add(Document(file_name="b.pdf", file_path="/b", file_size_bytes=200, user_id=1))
        db.commit()
        db.close()

        r = client.get("/api/v1/documents/stats/summary")
        assert r.json()["total_documents"] == 2


# ── Reprocess ─────────────────────────────────────────────────────────────────
class TestReprocess:
    def test_reprocess_resets_status(self, client):
        db = TestSessionLocal()
        doc = Document(
            file_name="old.pdf",
            file_path="/old.pdf",
            file_size_bytes=1000,
            status=ProcessingStatus.FAILED.value,
            error_message="Previous error",
            user_id=1,
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        with patch("api.routes.documents.process_document"):
            r = client.post(f"/api/v1/documents/{doc_id}/reprocess")

        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["error_message"] is None

    def test_reprocess_not_found_404(self, client):
        with patch("api.routes.documents.process_document"):
            assert client.post("/api/v1/documents/99999/reprocess").status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────
class TestDelete:
    def test_delete_document(self, client):
        db = TestSessionLocal()
        doc = Document(file_name="del.pdf", file_path="/del.pdf", file_size_bytes=500, user_id=1)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.delete(f"/api/v1/documents/{doc_id}")
        assert r.status_code == 204

        assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404

    def test_delete_not_found(self, client):
        assert client.delete("/api/v1/documents/99999").status_code == 404


# ── Feedback ──────────────────────────────────────────────────────────────────
class TestDocumentOwnership:
    def test_list_documents_excludes_other_users_documents(self, client):
        db = TestSessionLocal()
        db.add(Document(file_name="mine.pdf", file_path="/mine.pdf", file_size_bytes=100, user_id=1))
        db.add(Document(file_name="theirs.pdf", file_path="/theirs.pdf", file_size_bytes=100, user_id=2))
        db.commit()
        db.close()

        r = client.get("/api/v1/documents/")

        assert r.status_code == 200
        names = {doc["file_name"] for doc in r.json()}
        assert names == {"mine.pdf"}

    def test_get_document_returns_404_for_other_users_document(self, client):
        db = TestSessionLocal()
        doc = Document(file_name="private.pdf", file_path="/private.pdf", file_size_bytes=100, user_id=2)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.get(f"/api/v1/documents/{doc_id}")

        assert r.status_code == 404

    def test_delete_document_returns_404_for_other_users_document(self, client):
        db = TestSessionLocal()
        doc = Document(file_name="private.pdf", file_path="/private.pdf", file_size_bytes=100, user_id=2)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.delete(f"/api/v1/documents/{doc_id}")

        assert r.status_code == 404

    def test_export_csv_returns_404_for_other_users_document(self, client):
        db = TestSessionLocal()
        doc = Document(file_name="private.pdf", file_path="/private.pdf", file_size_bytes=100, user_id=2)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.get(f"/api/v1/documents/{doc_id}/export/csv")

        assert r.status_code == 404


class TestFeedback:
    def test_submit_feedback_success(self, client):
        db = TestSessionLocal()
        doc = Document(file_name="feed.pdf", file_path="/feed.pdf", file_size_bytes=500, user_id=1)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        # Send feedback request
        r = client.post(
            f"/api/v1/documents/{doc_id}/feedback",
            json={"rating": "positive", "comment": "Excellent extraction accuracy!"}
        )
        assert r.status_code == 200
        assert r.json()["success"] is True

        # Verify database record
        db = TestSessionLocal()
        from database.models import Feedback
        feedback_rec = db.query(Feedback).filter(Feedback.document_id == doc_id).first()
        assert feedback_rec is not None
        assert feedback_rec.rating == 1
        assert feedback_rec.comment == "Excellent extraction accuracy!"
        db.close()

    def test_submit_feedback_not_found(self, client):
        r = client.post(
            "/api/v1/documents/99999/feedback",
            json={"rating": "negative"}
        )
        assert r.status_code == 404

    def test_anonymous_feedback_cannot_target_registered_user_document(self, client):
        from api.main import app

        app.dependency_overrides[get_optional_user] = lambda: None
        db = TestSessionLocal()
        doc = Document(file_name="owned.pdf", file_path="/owned.pdf", file_size_bytes=500, user_id=1)
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.post(
            f"/api/v1/documents/{doc_id}/feedback",
            json={"rating": "positive"},
        )

        assert r.status_code == 404

    def test_anonymous_feedback_can_target_same_ip_anonymous_document(self, client):
        from api.main import app

        app.dependency_overrides[get_optional_user] = lambda: None
        db = TestSessionLocal()
        doc = Document(
            file_name="anonymous.pdf",
            file_path="/anonymous.pdf",
            file_size_bytes=500,
            user_id=None,
            ip_address="testclient",
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        r = client.post(
            f"/api/v1/documents/{doc_id}/feedback",
            json={"rating": "positive"},
        )

        assert r.status_code == 200
