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

import contextlib
import csv
import io
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth.core import get_current_user, get_optional_user
from auth.rate_limit import enforce_rate_limit, get_client_ip
from database.connection import get_db_for_fastapi
from database.models import Document, DocumentType, ExtractedField, Feedback, ProcessingStatus, User
from extractor.pipeline import _resolve_path, process_document
from upload.handler import save_upload, validate_file

router = APIRouter(prefix="/documents", tags=["documents"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────


class FieldResponse(BaseModel):
    id: int
    field_name: str
    field_value: str | None
    field_type: str
    confidence: float | None
    is_verified: bool
    model_config = ConfigDict(from_attributes=True)


class DocumentResponse(BaseModel):
    id: int
    file_name: str
    file_size_bytes: int | None
    status: str
    document_type: str
    page_count: int | None
    char_count: int | None
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    total_amount: float | None
    currency: str | None
    ai_confidence: float | None
    error_message: str | None
    uploaded_at: datetime
    processing_started_at: datetime | None
    processing_completed_at: datetime | None
    fields: list[FieldResponse] = []
    model_config = ConfigDict(from_attributes=True)

    @property
    def processing_time_seconds(self) -> float | None:
        if self.processing_started_at and self.processing_completed_at:
            return (self.processing_completed_at - self.processing_started_at).total_seconds()
        return None


class DocumentStatusResponse(BaseModel):
    id: int
    file_name: str
    status: str
    error_message: str | None
    uploaded_at: datetime
    processing_completed_at: datetime | None
    model_config = ConfigDict(from_attributes=True)


class UploadResponse(BaseModel):
    document_id: int
    file_name: str
    file_size_bytes: int
    status: str
    message: str


class FeedbackRequest(BaseModel):
    rating: str  # "positive" | "negative"
    comment: str | None = None


def _get_document_for_user(
    document_id: int,
    db: Session,
    current_user: User | None,
    request: Request,
) -> Document:
    """
    Fetch document checking ownership:
      - Logged-in user: must match user_id
      - Anonymous user: must have user_id=None and matching client IP address
    """
    if current_user:
        doc = (
            db.query(Document)
            .filter(Document.id == document_id, Document.user_id == current_user.id)
            .first()
        )
    else:
        ip = get_client_ip(request)
        doc = (
            db.query(Document)
            .filter(
                Document.id == document_id,
                Document.user_id.is_(None),
                Document.ip_address == ip,
            )
            .first()
        )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: str = Query(
        default="invoice", description="invoice|contract|receipt|report|other"
    ),
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> dict[str, Any]:
    """
    Upload a PDF document for AI extraction.

    Returns 202 Accepted immediately — extraction runs in the background.
    Poll GET /documents/{id}/status to check progress.
    Once status=completed, GET /documents/{id} returns extracted fields.
    """
    # Enforce rate limits
    ip = enforce_rate_limit(request, db, current_user)

    # Validate before touching DB
    validation = await validate_file(file)
    if not validation.valid:
        raise HTTPException(status_code=422, detail=validation.error)

    # Save to S3 or disk
    saved = await save_upload(file, validation)

    # Resolve document type
    try:
        doc_type = DocumentType(document_type.lower())
    except ValueError:
        doc_type = DocumentType.OTHER

    # Create DB record with owner mapping
    doc = Document(
        file_name=validation.original_name,
        file_path=str(saved.path),
        file_size_bytes=saved.size_bytes,
        document_type=doc_type.value,
        status=ProcessingStatus.QUEUED.value,
        user_id=current_user.id if current_user else None,
        ip_address=ip if not current_user else None,
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
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    document_type: str = Query(default="invoice"),
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> dict[str, Any]:
    """Upload multiple PDFs at once. Each processes independently."""
    results = []
    for file in files:
        validation = await validate_file(file)
        if not validation.valid:
            results.append({"file": file.filename, "error": validation.error})
            continue

        ip = enforce_rate_limit(request, db, current_user)
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
            user_id=current_user.id if current_user else None,
            ip_address=ip if not current_user else None,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        background_tasks.add_task(process_document, doc.id)
        results.append({"document_id": doc.id, "file": doc.file_name, "status": "queued"})

    return {"queued": len([r for r in results if "document_id" in r]), "results": results}


@router.get("/stats/summary")
def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_for_fastapi),
) -> dict[str, Any]:
    """Aggregate processing stats for the logged-in user."""
    total = (
        db.query(func.count(Document.id)).filter(Document.user_id == current_user.id).scalar() or 0
    )
    by_status = (
        db.query(Document.status, func.count(Document.id))
        .filter(Document.user_id == current_user.id)
        .group_by(Document.status)
        .all()
    )
    by_type = (
        db.query(Document.document_type, func.count(Document.id))
        .filter(Document.user_id == current_user.id)
        .group_by(Document.document_type)
        .all()
    )
    avg_confidence = (
        db.query(func.avg(Document.ai_confidence))
        .filter(Document.user_id == current_user.id)
        .scalar()
    )
    avg_total = (
        db.query(func.avg(Document.total_amount))
        .filter(Document.user_id == current_user.id, Document.total_amount.isnot(None))
        .scalar()
    )

    return {
        "total_documents": total,
        "by_status": {str(s): c for s, c in by_status},
        "by_type": {str(t): c for t, c in by_type},
        "avg_ai_confidence": round(float(avg_confidence or 0), 3),
        "avg_invoice_total": round(float(avg_total or 0), 2),
    }


@router.get("/export")
def export_csv(
    status: str | None = Query(default=None),
    document_type: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_for_fastapi),
) -> StreamingResponse:
    """Export document summaries as CSV for the logged-in user. Streams — no memory limit."""
    query = db.query(Document).filter(Document.user_id == current_user.id)
    if status:
        with contextlib.suppress(ValueError):
            query = query.filter(Document.status == status)
    if document_type:
        with contextlib.suppress(ValueError):
            query = query.filter(Document.document_type == document_type)

    docs = query.order_by(Document.uploaded_at.desc()).all()

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "id",
                "file_name",
                "status",
                "document_type",
                "vendor_name",
                "invoice_number",
                "invoice_date",
                "total_amount",
                "currency",
                "ai_confidence",
                "page_count",
                "uploaded_at",
            ]
        )
        yield output.getvalue()

        for doc in docs:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    doc.id,
                    doc.file_name,
                    doc.status,
                    doc.document_type,
                    doc.vendor_name,
                    doc.invoice_number,
                    doc.invoice_date,
                    doc.total_amount,
                    doc.currency,
                    doc.ai_confidence,
                    doc.page_count,
                    doc.uploaded_at.isoformat() if doc.uploaded_at else "",
                ]
            )
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
    field_name: str | None = Query(default=None, description="Restrict to specific field name"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_for_fastapi),
) -> list[dict[str, Any]]:
    """
    Full-text search across ExtractedField values.
    Returns matching document IDs and the field that matched for this user.
    """
    query = (
        db.query(ExtractedField)
        .join(Document)
        .filter(Document.user_id == current_user.id, ExtractedField.field_value.ilike(f"%{q}%"))
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
def get_status(
    document_id: int,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> Document:
    """
    Lightweight status check — no fields returned.
    Poll this every 2-5 seconds after upload until status=completed.
    """
    return _get_document_for_user(document_id, db, current_user, request)


@router.get("/{document_id}/fields", response_model=list[FieldResponse])
def get_fields(
    document_id: int,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> list[ExtractedField]:
    """All extracted fields for a document. Returns [] if not yet processed."""
    doc = _get_document_for_user(document_id, db, current_user, request)
    return doc.fields


@router.get("/{document_id}/download")
def download_document(
    document_id: int,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> Response:
    """Download the original PDF file from S3 or local disk."""
    doc = _get_document_for_user(document_id, db, current_user, request)

    if doc.file_path and doc.file_path.startswith("s3://"):
        from utils.s3 import download_file_bytes

        s3_path = doc.file_path[5:]  # Remove "s3://"
        parts = s3_path.split("/", 1)
        key = parts[1] if len(parts) == 2 else s3_path
        try:
            content = download_file_bytes(key)
            return Response(
                content=content,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{doc.file_name}"'},
            )
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"S3 download failed: {e}") from e
    else:
        # Local file
        file_path = _resolve_path(doc)
        if not file_path or not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found on local disk")
        return Response(
            content=file_path.read_bytes(),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{doc.file_name}"'},
        )


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> Document:
    """Full document with all extracted fields."""
    return _get_document_for_user(document_id, db, current_user, request)


@router.get("/", response_model=list[DocumentResponse])
def list_documents(
    status: str | None = Query(default=None),
    document_type: str | None = Query(default=None),
    limit: int = Query(default=20, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db_for_fastapi),
) -> list[Document]:
    """List all documents with optional status/type filters for the logged-in user."""
    query = db.query(Document).filter(Document.user_id == current_user.id)
    if status:
        with contextlib.suppress(ValueError):
            query = query.filter(Document.status == status)
    if document_type:
        with contextlib.suppress(ValueError):
            query = query.filter(Document.document_type == document_type)
    return query.order_by(Document.uploaded_at.desc()).offset(offset).limit(limit).all()


@router.post("/{document_id}/reprocess", response_model=DocumentStatusResponse)
async def reprocess(
    document_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> Document:
    """
    Re-run extraction on an existing document.
    Use when: you updated the prompt, or previous run failed.
    Deletes all existing ExtractedField rows first.
    """
    doc = _get_document_for_user(document_id, db, current_user, request)

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
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
) -> ExtractedField:
    """Mark a field as human-verified. Used in review workflows."""
    _get_document_for_user(document_id, db, current_user, request)

    field = (
        db.query(ExtractedField)
        .filter(
            ExtractedField.id == field_id,
            ExtractedField.document_id == document_id,
        )
        .first()
    )
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    field.is_verified = True
    db.commit()
    db.refresh(field)
    return field


@router.delete("/{document_id}", status_code=204, response_class=Response)
def delete_document(
    document_id: int,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
):
    doc = _get_document_for_user(document_id, db, current_user, request)

    # Delete S3 file if present
    if doc.file_path and doc.file_path.startswith("s3://"):
        from utils.s3 import delete_file

        s3_path = doc.file_path[5:]
        parts = s3_path.split("/", 1)
        key = parts[1] if len(parts) == 2 else s3_path
        delete_file(key)

    db.delete(doc)
    db.commit()
    # return nothing — 204 No Content


@router.post("/{document_id}/feedback")
def submit_feedback(
    document_id: int,
    req: FeedbackRequest,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db_for_fastapi),
):
    """Submit thumbs up/down feedback for an extraction."""
    ip = get_client_ip(request)
    query = db.query(Document).filter(Document.id == document_id)
    if current_user:
        query = query.filter(Document.user_id == current_user.id)
    else:
        query = query.filter(Document.user_id.is_(None), Document.ip_address == ip)
    doc = query.first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    rating_val = 1 if req.rating == "positive" else -1

    feedback = Feedback(
        document_id=document_id,
        user_id=current_user.id if current_user else None,
        ip_address=ip if not current_user else None,
        rating=rating_val,
        comment=req.comment,
    )
    db.add(feedback)
    db.commit()
    return {"success": True, "message": "Feedback submitted successfully"}
