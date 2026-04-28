from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer,
    String, Text, ForeignKey, JSON,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ProcessingStatus(PyEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentType(PyEnum):
    INVOICE = "invoice"
    CONTRACT = "contract"
    RECEIPT = "receipt"
    REPORT = "report"
    OTHER = "other"


class Document(Base):
    """
    Represents an uploaded file — the raw input.

    WHY separate Document from InvoiceExtraction?
    A document is always a document regardless of what we extract from it.
    Today we extract invoices. Tomorrow we extract contracts or resumes.
    The document table never changes. Only the extraction tables grow.
    Single Responsibility: this table tracks files, not fields.

    Status machine: pending → processing → completed | failed
    """
    __tablename__ = "documents"

    id              = Column(Integer, primary_key=True, index=True)
    file_name       = Column(String(500), nullable=False)           # original filename from user
    file_path       = Column(String(1000), nullable=False)          # path on disk (UUID-named)
    file_size_bytes = Column(Integer)
    mime_type       = Column(String(100), default="application/pdf")
    document_type   = Column(String(50), default="invoice")         # invoice | receipt | contract | other
    status          = Column(String(20), default="queued", index=True)
    # status values: queued | processing | completed | failed

    page_count      = Column(Integer, nullable=True)
    char_count      = Column(Integer, nullable=True)               # chars extracted from PDF
    raw_text        = Column(Text, nullable=True)                   # full extracted text (for re-processing)
    error_message   = Column(Text, nullable=True)

    # AI-extracted summary fields (fast querying without joining extracted_fields)
    vendor_name     = Column(String(500), nullable=True)
    invoice_number  = Column(String(200), nullable=True)
    invoice_date    = Column(String(50), nullable=True)
    due_date        = Column(String(50), nullable=True)
    total_amount    = Column(Float, nullable=True)
    currency        = Column(String(10), nullable=True, default="INR")
    ai_confidence   = Column(Float, nullable=True)                  # 0.0–1.0

    uploaded_at              = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    processing_started_at    = Column(DateTime, nullable=True)
    processing_completed_at  = Column(DateTime, nullable=True)

    # Relationships
    extraction = relationship("InvoiceExtraction", back_populates="document", uselist=False)
    fields = relationship("ExtractedField", back_populates="document", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Document id={self.id} name={self.file_name!r} status={self.status}>"


class InvoiceExtraction(Base):
    """
    Structured fields extracted from an invoice document via Claude API.

    WHY JSONB for line_items?
    Line items vary per invoice — 1 line or 100 lines, different fields.
    Storing as JSON avoids an entire separate table for a variable-length array.
    For analytics, PostgreSQL JSONB supports indexing and querying into JSON.

    WHY confidence_score?
    Claude sometimes hallucinates or admits uncertainty.
    The prompt asks Claude to score its own confidence 0-100.
    Low-confidence extractions (<60) should be flagged for human review.
    """
    __tablename__ = "invoice_extractions"

    id               = Column(Integer, primary_key=True, index=True)
    document_id      = Column(Integer, ForeignKey("documents.id"), unique=True, nullable=False)

    # Core invoice fields
    invoice_number   = Column(String(200))
    invoice_date     = Column(String(50))    # stored as string — date formats vary wildly
    due_date         = Column(String(50))
    currency         = Column(String(10), default="INR")

    # Parties
    vendor_name      = Column(String(500))
    vendor_address   = Column(Text)
    vendor_gstin     = Column(String(20))    # Indian GST number — critical for Indian invoices
    buyer_name       = Column(String(500))
    buyer_address    = Column(Text)
    buyer_gstin      = Column(String(20))

    # Financials
    subtotal         = Column(Float)
    tax_amount       = Column(Float)
    discount_amount  = Column(Float)
    total_amount     = Column(Float)

    # Line items — JSON array of {description, quantity, unit_price, total}
    line_items       = Column(JSON, default=list)

    # Extraction metadata
    confidence_score = Column(Integer)       # 0-100, Claude's self-assessed confidence
    model_used       = Column(String(100))   # "claude-sonnet-4-6" etc
    tokens_used      = Column(Integer)       # for cost tracking
    raw_llm_response = Column(Text)          # full Claude response — for debugging extractions
    extracted_at     = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    document = relationship("Document", back_populates="extraction")

    def __repr__(self) -> str:
        return (
            f"<InvoiceExtraction id={self.id} "
            f"invoice={self.invoice_number!r} total={self.total_amount}>"
        )


class ExtractedField(Base):
    """
    Generic key-value store for extracted document fields.
    Flexible: works for any document type without schema changes.
    Coexists with InvoiceExtraction (which stores structured invoice data).

    WHY both tables?
    InvoiceExtraction = fast analytics (query total_amount, vendor_name directly)
    ExtractedField    = flexible storage (any document type, any field name)
    Pipeline writes to both — InvoiceExtraction for invoices, ExtractedField for all types.
    """
    __tablename__ = "extracted_fields"

    id          = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False, index=True)
    field_name  = Column(String(200), nullable=False)
    field_value = Column(Text, nullable=True)
    field_type  = Column(String(50), default="string")  # string | number | date | list_item | object
    confidence  = Column(Float, nullable=True)
    is_verified = Column(Boolean, default=False)

    document = relationship("Document", back_populates="fields")

    def __repr__(self) -> str:
        return f"<ExtractedField {self.field_name}={self.field_value!r}>"
