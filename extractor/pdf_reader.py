"""
extractor/pdf_reader.py

Single source of truth for PDF text extraction.
All extractors import from here — never from cerebras_extractor.
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger("docai.pdf_reader")


def extract_text_from_pdf(pdf_bytes: bytes, max_pages: int = 20) -> str:
    """
    Extract text from PDF bytes using pdfplumber with PyMuPDF fallback.

    Args:
        pdf_bytes: Raw PDF file bytes.
        max_pages: Maximum pages to process (invoices are never >20 pages).

    Returns:
        Extracted text string. Empty string if extraction fails.
    """
    text = _extract_with_pdfplumber(pdf_bytes, max_pages)
    if text and len(text.strip()) >= 50:
        return text

    logger.warning("pdfplumber extraction insufficient, trying PyMuPDF")
    text = _extract_with_pymupdf(pdf_bytes, max_pages)
    if text and len(text.strip()) >= 50:
        return text

    logger.error("Both PDF extractors failed or returned insufficient text")
    return ""


def _extract_with_pdfplumber(pdf_bytes: bytes, max_pages: int) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = pdf.pages[:max_pages]
            return "\n".join(page.extract_text() or "" for page in pages)
    except Exception as exc:
        logger.error("pdfplumber failed: %s", exc)
        return ""


def _extract_with_pymupdf(pdf_bytes: bytes, max_pages: int) -> str:
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = list(doc)[:max_pages]
        return "\n".join(page.get_text() for page in pages)
    except Exception as exc:
        logger.error("PyMuPDF failed: %s", exc)
        return ""
