"""
tests/test_pipeline.py — Unit tests for the extraction pipeline's
resilience, threshold fallback behavior, and consecutive failure rate-limiting.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import extractor.pipeline
from database.models import Base, Document
from extractor.pipeline import process_document

TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    extractor.pipeline.CONSECUTIVE_TIER1_FAILURES = 0
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def mock_db_session():
    with patch("extractor.pipeline.SessionLocal", TestSessionLocal):
        yield


@pytest.fixture
def mock_resolve_path():
    with patch("extractor.pipeline._resolve_path") as mock_res:
        mock_path = MagicMock()
        mock_path.read_bytes.return_value = b"%PDF-1.4 mock pdf text"
        mock_res.return_value = mock_path
        yield mock_res


@pytest.mark.asyncio
async def test_groq_high_confidence(mock_db_session, mock_resolve_path):
    # Setup document in DB
    db = TestSessionLocal()
    doc = Document(
        id=101,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    groq_mock_result = {
        "vendor_name": "Groq Vendor",
        "invoice_number": "G123",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.92,
    }

    with patch("extractor.pipeline.extract_groq", return_value=groq_mock_result) as mock_groq, \
         patch("extractor.pipeline.extract_mistral") as mock_mistral, \
         patch("extractor.pipeline.extract_gemini") as mock_gemini:

        await process_document(101)

        mock_groq.assert_called_once()
        mock_mistral.assert_not_called()
        mock_gemini.assert_not_called()

    # Verify db status
    db = TestSessionLocal()
    doc_after = db.query(Document).filter(Document.id == 101).first()
    assert doc_after.status == "completed"
    assert doc_after.vendor_name == "Groq Vendor"
    assert doc_after.ai_confidence == 0.92
    assert extractor.pipeline.CONSECUTIVE_TIER1_FAILURES == 0
    db.close()


@pytest.mark.asyncio
async def test_groq_low_mistral_high(mock_db_session, mock_resolve_path):
    db = TestSessionLocal()
    doc = Document(
        id=102,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    groq_mock_result = {
        "vendor_name": "Groq Vendor",
        "invoice_number": "G123",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.85,
    }
    mistral_mock_result = {
        "vendor_name": "Mistral Vendor",
        "invoice_number": "M456",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.96,
    }

    with patch("extractor.pipeline.extract_groq", return_value=groq_mock_result) as mock_groq, \
         patch("extractor.pipeline.extract_mistral", return_value=mistral_mock_result) as mock_mistral, \
         patch("extractor.pipeline.extract_gemini") as mock_gemini:

        await process_document(102)

        mock_groq.assert_called_once()
        mock_mistral.assert_called_once()
        mock_gemini.assert_not_called()

    # Verify db status
    db = TestSessionLocal()
    doc_after = db.query(Document).filter(Document.id == 102).first()
    assert doc_after.status == "completed"
    assert doc_after.vendor_name == "Mistral Vendor"
    assert doc_after.ai_confidence == 0.96
    assert extractor.pipeline.CONSECUTIVE_TIER1_FAILURES == 0
    db.close()


@pytest.mark.asyncio
async def test_gemini_exception_ignored_retains_mistral(mock_db_session, mock_resolve_path):
    db = TestSessionLocal()
    doc = Document(
        id=103,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    groq_mock_result = {
        "vendor_name": "Groq Vendor",
        "invoice_number": "G123",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.40,
    }
    mistral_mock_result = {
        "vendor_name": "Mistral Vendor",
        "invoice_number": "M456",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.88,
    }

    with patch("extractor.pipeline.extract_groq", return_value=groq_mock_result) as mock_groq, \
         patch("extractor.pipeline.extract_mistral", return_value=mistral_mock_result) as mock_mistral, \
         patch("extractor.pipeline.extract_gemini", side_effect=Exception("Gemini Rate Limit")) as mock_gemini:

        await process_document(103)

        assert mock_groq.call_count >= 1
        assert mock_mistral.call_count >= 1
        assert mock_gemini.call_count >= 1

    db = TestSessionLocal()
    doc_after = db.query(Document).filter(Document.id == 103).first()
    assert doc_after.status == "completed"
    assert doc_after.vendor_name == "Mistral Vendor"
    assert doc_after.ai_confidence == 0.88
    db.close()


@pytest.mark.asyncio
async def test_gemini_zero_confidence_ignored_retains_mistral(mock_db_session, mock_resolve_path):
    db = TestSessionLocal()
    doc = Document(
        id=104,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    groq_mock_result = {
        "vendor_name": "Groq Vendor",
        "invoice_number": "G123",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.40,
    }
    mistral_mock_result = {
        "vendor_name": "Mistral Vendor",
        "invoice_number": "M456",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.88,
    }
    gemini_mock_result = {
        "vendor_name": "Gemini Vendor",
        "invoice_number": None,
        "invoice_date": None,
        "total_amount": None,
        "currency": None,
        "ai_confidence": 0.00,
    }

    with patch("extractor.pipeline.extract_groq", return_value=groq_mock_result) as mock_groq, \
         patch("extractor.pipeline.extract_mistral", return_value=mistral_mock_result) as mock_mistral, \
         patch("extractor.pipeline.extract_gemini", return_value=gemini_mock_result) as mock_gemini:

        await process_document(104)

        assert mock_groq.call_count >= 1
        assert mock_mistral.call_count >= 1
        assert mock_gemini.call_count >= 1

    db = TestSessionLocal()
    doc_after = db.query(Document).filter(Document.id == 104).first()
    assert doc_after.status == "completed"
    assert doc_after.vendor_name == "Mistral Vendor"
    assert doc_after.ai_confidence == 0.88
    db.close()


@pytest.mark.asyncio
async def test_consecutive_failures_sleep(mock_db_session, mock_resolve_path):
    extractor.pipeline.CONSECUTIVE_TIER1_FAILURES = 1

    db = TestSessionLocal()
    doc = Document(
        id=105,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    groq_mock_result = {
        "vendor_name": "Groq Vendor",
        "invoice_number": "G123",
        "invoice_date": "2026-05-31",
        "total_amount": 100.0,
        "currency": "USD",
        "ai_confidence": 0.95,
    }

    with patch("extractor.pipeline.extract_groq", return_value=groq_mock_result) as mock_groq, \
         patch("extractor.pipeline.extract_mistral") as mock_mistral, \
         patch("extractor.pipeline.extract_gemini") as mock_gemini, \
         patch("asyncio.sleep") as mock_sleep:

        await process_document(105)

        mock_sleep.assert_called_once_with(1)
        mock_groq.assert_called_once()
        mock_mistral.assert_not_called()
        mock_gemini.assert_not_called()

    assert extractor.pipeline.CONSECUTIVE_TIER1_FAILURES == 0


@pytest.mark.asyncio
async def test_groq_failure_increments_counter(mock_db_session, mock_resolve_path):
    db = TestSessionLocal()
    doc = Document(
        id=106,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    with patch("extractor.pipeline.extract_groq", side_effect=Exception("Groq error")), \
         patch("extractor.pipeline.extract_mistral", return_value=None), \
         patch("extractor.pipeline.extract_gemini", return_value=None):

        await process_document(106)

    assert extractor.pipeline.CONSECUTIVE_TIER1_FAILURES == 1


@pytest.mark.asyncio
async def test_provider_busy_skips_to_next(mock_db_session, mock_resolve_path):
    from extractor.pipeline import GROQ_LOCK, MISTRAL_LOCK
    await GROQ_LOCK.acquire()
    await MISTRAL_LOCK.acquire()

    db = TestSessionLocal()
    doc = Document(
        id=107,
        file_name="mock_path.pdf",
        file_path="mock_path.pdf",
        status="queued",
        page_count=1,
        char_count=100,
    )
    db.add(doc)
    db.commit()
    db.close()

    gemini_mock_result = {
        "vendor_name": "Gemini Vendor Only",
        "invoice_number": "GEM-99",
        "invoice_date": "2026-05-31",
        "total_amount": 250.0,
        "currency": "USD",
        "ai_confidence": 0.98,
    }

    try:
        with patch("extractor.pipeline.extract_groq") as mock_groq, \
             patch("extractor.pipeline.extract_mistral") as mock_mistral, \
             patch("extractor.pipeline.extract_gemini", return_value=gemini_mock_result) as mock_gemini:

            await process_document(107)

            mock_groq.assert_not_called()
            mock_mistral.assert_not_called()
            mock_gemini.assert_called_once()
    finally:
        GROQ_LOCK.release()
        MISTRAL_LOCK.release()

    # Verify db status (retrieved from Gemini since others were skipped)
    db = TestSessionLocal()
    doc_after = db.query(Document).filter(Document.id == 107).first()
    assert doc_after.status == "completed"
    assert doc_after.vendor_name == "Gemini Vendor Only"
    assert doc_after.ai_confidence == 0.98
    db.close()
