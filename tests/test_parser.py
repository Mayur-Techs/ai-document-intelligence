"""
tests/test_parser.py — Tests for parser/extractor.py.

Pure function tests — no DB, no API, no Claude calls.
Run: pytest tests/test_parser.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from parser.extractor import ExtractionResult, extract_text, truncate_text

# Realistic extracted text — must be > 50 chars to pass sparse-text threshold
REALISTIC_TEXT = (
    "INVOICE | Sharma Freight Solutions Pvt Ltd | "
    "Invoice No: INV-2026-04892 | Date: 15 April 2026 | "
    "Total Amount: Rs 1,97,355.00 | GST: 30,105.00 | "
    "Subtotal: 1,67,250.00 | Payment Terms: 30 days net"
)


def _make_plumber_mock(text: str, page_count: int = 1):
    """Build a correctly-structured pdfplumber mock with full context-manager protocol."""
    mock_page = MagicMock()
    mock_page.extract_text.return_value = text
    mock_page.extract_tables.return_value = []

    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = [mock_page] * page_count
    return mock_pdf


def _make_fitz_mock(text: str, page_count: int = 1):
    """Build a correctly-structured PyMuPDF mock."""
    mock_page = MagicMock()
    mock_page.get_text.return_value = text

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page] * page_count))
    mock_doc.__len__ = MagicMock(return_value=page_count)
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    return mock_doc


class TestTruncateText:
    """Truncation prevents blowing Claude's context window."""

    def test_short_text_unchanged(self):
        assert truncate_text("Hello world", max_chars=100) == "Hello world"

    def test_long_text_truncated(self):
        result = truncate_text("A" * 10_000, max_chars=8_000)
        assert len(result) <= 8_100  # small buffer for truncation notice

    def test_truncation_adds_notice(self):
        result = truncate_text("X" * 10_000, max_chars=100)
        assert "[truncated" in result.lower()

    def test_empty_text_unchanged(self):
        assert truncate_text("", max_chars=100) == ""

    def test_exactly_at_limit_unchanged(self):
        text = "B" * 8_000
        assert truncate_text(text, max_chars=8_000) == text


class TestExtractText:
    """Tests for extract_text() — mocks pdfplumber/fitz to isolate our logic."""

    def test_successful_extraction_returns_text(self, tmp_path, sample_pdf_bytes):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(sample_pdf_bytes)

        with patch("parser.extractor.pdfplumber") as mock_plumber:
            mock_plumber.open.return_value = _make_plumber_mock(REALISTIC_TEXT)
            result = extract_text(str(pdf_file))

        assert isinstance(result, ExtractionResult)
        assert result.success is True
        assert "Sharma Freight" in result.text
        assert result.page_count == 1
        assert result.error is None

    def test_file_not_found_returns_error_result(self):
        result = extract_text("/nonexistent/path/document.pdf")
        assert result.success is False
        assert result.text == ""
        assert result.error is not None
        assert result.page_count == 0

    def test_empty_pdf_returns_error(self, tmp_path, sample_pdf_bytes):
        """Image-only PDF — both extractors return empty text → error result, no exception."""
        pdf_file = tmp_path / "scanned.pdf"
        pdf_file.write_bytes(sample_pdf_bytes)

        with patch("parser.extractor.pdfplumber") as mock_plumber, \
             patch("parser.extractor.fitz") as mock_fitz:
            mock_plumber.open.return_value = _make_plumber_mock("")  # empty
            mock_fitz.open.return_value = _make_fitz_mock("")        # also empty
            result = extract_text(str(pdf_file))

        assert result.success is False
        assert result.error is not None
        # Error message must communicate the root cause clearly
        assert any(kw in result.error.lower() for kw in ("no text", "empty", "image", "scanned"))

    def test_pdfplumber_failure_falls_back_to_pymupdf(self, tmp_path, sample_pdf_bytes):
        """pdfplumber exception → PyMuPDF fallback → success if fitz returns text."""
        pdf_file = tmp_path / "fallback.pdf"
        pdf_file.write_bytes(sample_pdf_bytes)

        with patch("parser.extractor.pdfplumber") as mock_plumber, \
             patch("parser.extractor.fitz") as mock_fitz:
            mock_plumber.open.side_effect = Exception("pdfplumber failed")
            mock_fitz.open.return_value = _make_fitz_mock(REALISTIC_TEXT)
            result = extract_text(str(pdf_file))

        assert result.success is True
        assert "Sharma Freight" in result.text

    def test_both_extractors_fail_returns_error(self, tmp_path, sample_pdf_bytes):
        """Both fail → error result, not an unhandled exception."""
        pdf_file = tmp_path / "broken.pdf"
        pdf_file.write_bytes(sample_pdf_bytes)

        with patch("parser.extractor.pdfplumber") as mock_plumber, \
             patch("parser.extractor.fitz") as mock_fitz:
            mock_plumber.open.side_effect = Exception("corrupt PDF header")
            mock_fitz.open.side_effect = Exception("cannot repair")
            result = extract_text(str(pdf_file))

        assert result.success is False
        assert result.error is not None
