"""
api/routes/documents.py — Document upload, status, and query endpoints.

13 endpoints covering the full document lifecycle:
  POST   /documents/upload          — upload PDF, returns job ID immediately
  GET    /documents/{id}            — get document + extracted fields
  GET    /documents/{id}/status     — lightweight status poll (no fields)
  GET    /documents/{id}/fields     — all extracted fields for a document
  GET    /documents/                — list all documents with filters
  GET    /documents/stats/summary   — aggregate processing stats
  GET    /documents/export          — CSV export of document summaries
  DELETE /documents/{id}            — delete document + fields
  POST   /documents/{id}/reprocess  — re-run extraction on existing document
  GET    /documents/search          — full-text search across extracted fields
  PATCH  /documents/{id}/verify     — mark a specific field as human-verified
  GET    /documents/{id}/download   — download original PDF
  POST   /documents/batch/upload    — upload multiple PDFs at once
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.connection import get_db_for_fastapi
from database.models import Document, DocumentType, ExtractedField, ProcessingStatus
from extractor.pipeline import process_document
from upload.handler import save_upload, validate_file

router = APIRouter(prefix="/documents", tags=["documents"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class FieldResponse(BaseModel):
    id: int
    field_name: str
    field_value: Optional[str]
    field_type: str
    confidence: Optional[float]
    is_verified: bool
    model_config = ConfigDict(from_attributes=True)


class DocumentResponse(BaseModel):
    id: int
    file_name: str
    file_size_bytes: Optional[int]
    status: str
    document_type: str
    page_count: Optional[int]
    char_count: Optional[int]
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    due_date: Optional[str]
    total_amount: Optional[float]
    currency: Optional[str]
    ai_confidence: Optional[float]
    error_message: Optional[str]
    uploaded_at: datetime
    processing_started_at: Optional[datetime]
    processing_completed_at: Optional[datetime]
    fields: list[FieldResponse] = []
    model_config = ConfigDict(from_attributes=True)

    @property
    def processing_time_seconds(self) -> Optional[float]:
        if self.processing_started_at and self.processing_completed_at:
            return (self.processing_completed_at - self.processing_started_at).total_seconds()
        return None


class DocumentStatusResponse(BaseModel):
    id: int
    file_name: str
    status: str
    error_message: Optional[str]
    uploaded_at: datetime
    processing_completed_at: Optional[datetime]
    model_config = ConfigDict(from_attributes=True)


class UploadResponse(BaseModel):
    document_id: int
    file_name: str
    file_size_bytes: int
    status: str
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: str = Query(default="invoice", description="invoice|contract|receipt|report|other"),
    db: Session = Depends(get_db_for_fastapi),
) -> dict[str, Any]:
    """
    Upload a PDF document for AI extraction.

    Returns 202 Accepted immediately — extraction runs in the background.
    Poll GET /documents/{id}/status to check progress.
    Once status=completed, GET /documents/{id} returns extracted fields.

    WHY 202 and not 200?
    HTTP 202 = "I accepted the request and will process it asynchronously."
    HTTP 200 = "I processed it and here's the result."
    Returning 200 here would imply the extraction is done, which it isn't.
    """
    # Validate before touching DB
    validation = await validate_file(file)
    if not validation.valid:
        raise HTTPException(status_code=422, detail=validation.error)

    # Save to disk
    saved = await save_upload(file, validation)

    # Resolve document type
    try:
        doc_type = DocumentType(document_type.lower())
    except ValueError:
        doc_type = DocumentType.OTHER

    # Create DB record
    doc = Document(
        file_name=validation.original_name,
        file_path=str(saved.path),
        file_size_bytes=saved.size_bytes,
        document_type=doc_type.value,
        status=ProcessingStatus.QUEUED.value,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Trigger async extraction pipeline
    background_tasks.add_task(process_document, doc.id)

    return {
        "document_id": doc.id,
        "file_name": doc.file_name,
        "file_size_bytes": doc.file_size_bytes,
        "status": doc.status,
        "message": f"Document queued for extraction. Poll GET /documents/{doc.id}/status",
    }


@router.post("/batch/upload", status_code=202)
async def batch_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    document_type: str = Query(default="invoice"),
    db: Session = Depends(get_db_for_fastapi),
) -> dict[str, Any]:
    """Upload multiple PDFs at once. Each processes independently."""
    results = []
    for file in files:
        validation = await validate_file(file)
        if not validation.valid:
            results.append({"file": file.filename, "error": validation.error})
            continue

        saved = await save_upload(file, validation)
        try:
            doc_type = DocumentType(document_type.lower())
        except ValueError:
            doc_type = DocumentType.OTHER

        doc = Document(
            file_name=validation.original_name,
            file_path=str(saved.path),
            file_size_bytes=saved.size_bytes,
            document_type=doc_type.value,
            status=ProcessingStatus.QUEUED.value,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        background_tasks.add_task(process_document, doc.id)
        results.append({"document_id": doc.id, "file": doc.file_name, "status": "queued"})

    return {"queued": len([r for r in results if "document_id" in r]), "results": results}


@router.get("/stats/summary")
def get_stats(db: Session = Depends(get_db_for_fastapi)) -> dict[str, Any]:
    """Aggregate processing stats — total docs, by status, avg confidence, avg time."""
    total = db.query(func.count(Document.id)).scalar() or 0
    by_status = (
        db.query(Document.status, func.count(Document.id))
        .group_by(Document.status)
        .all()
    )
    by_type = (
        db.query(Document.document_type, func.count(Document.id))
        .group_by(Document.document_type)
        .all()
    )
    avg_confidence = db.query(func.avg(Document.ai_confidence)).scalar()
    avg_total = db.query(func.avg(Document.total_amount)).filter(
        Document.total_amount.isnot(None)
    ).scalar()

    return {
        "total_documents": total,
        "by_status": {str(s): c for s, c in by_status},
        "by_type": {str(t): c for t, c in by_type},
        "avg_ai_confidence": round(float(avg_confidence or 0), 3),
        "avg_invoice_total": round(float(avg_total or 0), 2),
    }


@router.get("/export")
def export_csv(
    status: Optional[str] = Query(default=None),
    document_type: Optional[str] = Query(default=None),
    db: Session = Depends(get_db_for_fastapi),
) -> StreamingResponse:
    """Export document summaries as CSV. Streams — no memory limit."""
    query = db.query(Document)
    if status:
        try:
            query = query.filter(Document.status == status)
        except ValueError:
            pass
    if document_type:
        try:
            query = query.filter(Document.document_type == document_type)
        except ValueError:
            pass

    docs = query.order_by(Document.uploaded_at.desc()).all()

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "file_name", "status", "document_type",
            "vendor_name", "invoice_number", "invoice_date",
            "total_amount", "currency", "ai_confidence",
            "page_count", "uploaded_at",
        ])
        yield output.getvalue()

        for doc in docs:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                doc.id, doc.file_name, doc.status, doc.document_type,
                doc.vendor_name, doc.invoice_number, doc.invoice_date,
                doc.total_amount, doc.currency, doc.ai_confidence,
                doc.page_count, doc.uploaded_at.isoformat() if doc.uploaded_at else "",
            ])
            yield output.getvalue()

    filename = f"documents_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/search")
def search_documents(
    q: str = Query(..., description="Search across extracted field values"),
    field_name: Optional[str] = Query(default=None, description="Restrict to specific field name"),
    db: Session = Depends(get_db_for_fastapi),
) -> list[dict[str, Any]]:
    """
    Full-text search across ExtractedField values.
    Returns matching document IDs and the field that matched.
    """
    query = db.query(ExtractedField).filter(
        ExtractedField.field_value.ilike(f"%{q}%")
    )
    if field_name:
        query = query.filter(ExtractedField.field_name == field_name)

    matches = query.limit(50).all()
    return [
        {
            "document_id": m.document_id,
            "field_name": m.field_name,
            "field_value": m.field_value,
            "match_query": q,
        }
        for m in matches
    ]


@router.get("/{document_id}/status", response_model=DocumentStatusResponse)
def get_status(document_id: int, db: Session = Depends(get_db_for_fastapi)) -> Document:
    """
    Lightweight status check — no fields returned.
    Poll this every 2–5 seconds after upload until status=completed.
    """
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return doc


@router.get("/{document_id}/fields", response_model=list[FieldResponse])
def get_fields(document_id: int, db: Session = Depends(get_db_for_fastapi)) -> list[ExtractedField]:
    """All extracted fields for a document. Returns [] if not yet processed."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return doc.fields


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(document_id: int, db: Session = Depends(get_db_for_fastapi)) -> Document:
    """Full document with all extracted fields."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return doc


@router.get("/", response_model=list[DocumentResponse])
def list_documents(
    status: Optional[str] = Query(default=None),
    document_type: Optional[str] = Query(default=None),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db_for_fastapi),
) -> list[Document]:
    """List all documents with optional status/type filters."""
    query = db.query(Document)
    if status:
        try:
            query = query.filter(Document.status == status)
        except ValueError:
            pass
    if document_type:
        try:
            query = query.filter(Document.document_type == document_type)
        except ValueError:
            pass
    return query.order_by(Document.uploaded_at.desc()).offset(offset).limit(limit).all()


@router.post("/{document_id}/reprocess", response_model=DocumentStatusResponse)
async def reprocess(
    document_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db_for_fastapi),
) -> Document:
    """
    Re-run extraction on an existing document.
    Use when: you updated the prompt, or previous run failed.
    Deletes all existing ExtractedField rows first.
    """
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Clear previous extraction
    db.query(ExtractedField).filter(ExtractedField.document_id == document_id).delete()
    doc.status = ProcessingStatus.QUEUED.value
    doc.error_message = None
    doc.vendor_name = None
    doc.invoice_number = None
    doc.total_amount = None
    doc.ai_confidence = None
    db.commit()

    background_tasks.add_task(process_document, document_id)
    return doc


@router.patch("/{document_id}/fields/{field_id}/verify", response_model=FieldResponse)
def verify_field(
    document_id: int,
    field_id: int,
    db: Session = Depends(get_db_for_fastapi),
) -> ExtractedField:
    """Mark a field as human-verified. Used in review workflows."""
    field = db.query(ExtractedField).filter(
        ExtractedField.id == field_id,
        ExtractedField.document_id == document_id,
    ).first()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    field.is_verified = True
    db.commit()
    db.refresh(field)
    return field


# FIXED
from fastapi import Response  # add this to imports at top if not there

@router.delete("/{document_id}", status_code=204, response_class=Response)
def delete_document(document_id: int, db: Session = Depends(get_db_for_fastapi)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    db.delete(doc)
    db.commit()
    # return nothing — 204 No Content
