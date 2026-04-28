"""
tests/conftest.py — Shared fixtures for AI Document Intelligence tests.

Same patterns as System 1:
  - StaticPool for SQLite in-memory connection sharing
  - patch("database.connection.Base") to mock init_db
  - dependency_overrides for DB session swap

Fixtures:
  sample_pdf_bytes      — minimal valid PDF as bytes (no PyMuPDF required)
  sample_extracted_text — realistic invoice text for LLM testing
  sample_llm_response   — realistic Claude API JSON response
  sample_document_data  — clean document dict for DB insertion
"""
from __future__ import annotations

import pytest


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """
    Minimal valid PDF binary. Used to test file validation and upload handler
    without requiring a real PDF file or PyMuPDF installed.

    This is the smallest possible valid PDF structure (27 bytes + content).
    It has 1 page with the text 'Invoice Test Document'.
    """
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
        b"/Contents 4 0 R/Resources<</Font<</F1<</Type/Font"
        b"/Subtype/Type1/BaseFont/Helvetica>>>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>\nstream\n"
        b"BT /F1 12 Tf 100 700 Td (Invoice Test Document) Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n"
        b"trailer<</Size 5/Root 1 0 R>>\n"
        b"startxref\n0\n%%EOF"
    )


@pytest.fixture
def sample_extracted_text() -> str:
    """
    Realistic invoice text as would be extracted from a PDF.
    Used to test the LLM extraction prompt and response parsing
    without making real API calls.
    """
    return """
    INVOICE

    Vendor: Sharma Freight Solutions Pvt Ltd
    Address: 45 Industrial Area, Phase 2, Pune - 411057

    Bill To: Acme Manufacturing Ltd
    Invoice No: INV-2026-04892
    Invoice Date: 15 April 2026
    Due Date: 15 May 2026

    Line Items:
    1. Ocean Freight - Mumbai to Rotterdam     ₹ 1,42,500.00
    2. Port Handling Charges                   ₹   8,250.00
    3. Custom Clearance Documentation          ₹   4,500.00
    4. Inland Transport - Factory to Port      ₹  12,000.00

    Subtotal:                                  ₹ 1,67,250.00
    GST @ 18%:                                 ₹  30,105.00
    GRAND TOTAL:                               ₹ 1,97,355.00

    Payment Terms: 30 days net
    Bank: HDFC Bank | Account: 50200012345678 | IFSC: HDFC0001234
    """.strip()


@pytest.fixture
def sample_llm_response() -> dict:
    """
    Realistic Claude API JSON response for invoice extraction.
    Used in tests that mock httpx.AsyncClient.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": """{
  "vendor_name": "Sharma Freight Solutions Pvt Ltd",
  "invoice_number": "INV-2026-04892",
  "invoice_date": "2026-04-15",
  "due_date": "2026-05-15",
  "total_amount": 197355.00,
  "currency": "INR",
  "line_items": [
    {"description": "Ocean Freight Mumbai to Rotterdam", "amount": 142500.00},
    {"description": "Port Handling Charges", "amount": 8250.00},
    {"description": "Custom Clearance Documentation", "amount": 4500.00},
    {"description": "Inland Transport Factory to Port", "amount": 12000.00}
  ],
  "subtotal": 167250.00,
  "tax_amount": 30105.00,
  "confidence_score": 94
}"""
            }
        ]
    }


@pytest.fixture
def sample_document_data() -> dict:
    """Clean document dict matching Document model fields."""
    return {
        "file_name": "invoice_sharma_freight_apr2026.pdf",
        "file_path": "/data/raw/abc123.pdf",
        "file_size_bytes": 45678,
        "vendor_name": "Sharma Freight Solutions Pvt Ltd",
        "invoice_number": "INV-2026-04892",
        "invoice_date": "2026-04-15",
        "total_amount": 197355.00,
        "currency": "INR",
        "ai_confidence": 0.94,
        "page_count": 2,
        "char_count": 842,
    }
