"""
extractor/pipeline.py
──────────────────────
Entry point called by the FastAPI background task after every upload.

Called as:  await process_document(document_id)

Key fixes vs previous version:
  - Uses SessionLocal() directly instead of get_db() context manager
    (get_db() is a FastAPI generator/yield dependency — cannot be used with `with`)
  - Full exception logging so errors appear in Render logs, not silent fails
  - Tries multiple possible file-path column names defensively
  - Self-contained DB session management — no dependency on how routes use get_db
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Document, ExtractedField
from extractor.groq_extractor import extract_primary, extract_fallback, needs_fallback
from extractor.field_sanitizer import merge_best

logger = logging.getLogger("docai.pipeline")

CONFIDENCE_THRESHOLD = 0.95


# ─────────────────────────────────────────────────────────────
#  Safe DB session — never use get_db() here, it's a FastAPI
#  generator and breaks outside of request context
# ─────────────────────────────────────────────────────────────

def _get_session() -> Session:
    """Return a plain SQLAlchemy session. Caller must call .close()."""
    return SessionLocal()


# ─────────────────────────────────────────────────────────────
#  Resolve the file path from the document record
#  (handles multiple possible column names defensively)
# ─────────────────────────────────────────────────────────────

def _get_file_path(doc) -> str | None:
    """Try every possible attribute name for the stored file path."""
    for attr in ("file_path", "upload_path", "path", "file_name", "filename"):
        val = getattr(doc, attr, None)
        if val:
            return str(val)
    return None


# ─────────────────────────────────────────────────────────────
#  DB write helpers
# ─────────────────────────────────────────────────────────────

def _update_document(db: Session, document_id: int, result: dict, status: str) -> None:
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        logger.error("_update_document: document id=%d not found", document_id)
        return

    doc.status         = status
    doc.vendor_name    = result.get("vendor_name")
    doc.invoice_number = result.get("invoice_number")
    doc.invoice_date   = result.get("invoice_date")
    doc.due_date       = result.get("due_date")
    doc.total_amount   = result.get("total_amount")
    doc.currency       = result.get("currency") or "INR"
    doc.ai_confidence  = result.get("ai_confidence", 0.0)
    doc.error_message  = None
    db.commit()
    logger.info(
        "Document id=%d → status=%s  confidence=%.2f",
        document_id, status, doc.ai_confidence or 0,
    )


def _write_fields(db: Session, document_id: int, result: dict) -> None:
    # Clear any previous extraction
    db.query(ExtractedField).filter(
        ExtractedField.document_id == document_id
    ).delete(synchronize_session=False)

    confidence = result.get("ai_confidence", 0.0)

    scalar_fields = [
        ("vendor_name",         result.get("vendor_name"),         "string"),
        ("vendor_gstin",        result.get("vendor_gstin"),        "string"),
        ("buyer_name",          result.get("buyer_name"),          "string"),
        ("buyer_gstin",         result.get("buyer_gstin"),         "string"),
        ("invoice_number",      result.get("invoice_number"),      "string"),
        ("invoice_date",        result.get("invoice_date"),        "string"),
        ("due_date",            result.get("due_date"),            "string"),
        ("currency",            result.get("currency"),            "string"),
        ("subtotal",            result.get("subtotal"),            "number"),
        ("tax_amount",          result.get("tax_amount"),          "number"),
        ("total_amount",        result.get("total_amount"),        "number"),
        ("bank_ifsc",           result.get("bank_ifsc"),           "string"),
        ("bank_account_number", result.get("bank_account_number"), "string"),
        ("bank_name",           result.get("bank_name"),           "string"),
    ]

    written = 0
    for field_name, field_value, field_type in scalar_fields:
        if field_value is None:
            continue
        db.add(ExtractedField(
            document_id=document_id,
            field_name=field_name,
            field_value=str(field_value),
            field_type=field_type,
            confidence=confidence,
            is_verified=False,
        ))
        written += 1

    for idx, item in enumerate(result.get("line_items", []), start=1):
        db.add(ExtractedField(
            document_id=document_id,
            field_name=f"line_items_{idx}",
            field_value=json.dumps(item, ensure_ascii=False),
            field_type="list_item",
            confidence=confidence,
            is_verified=False,
        ))

    db.commit()
    logger.info(
        "Wrote %d fields + %d line items for document id=%d",
        written, len(result.get("line_items", [])), document_id,
    )


def _mark_failed(document_id: int, reason: str) -> None:
    """Write failed status to DB — has its own session so it always works."""
    db = _get_session()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = "failed"
            doc.error_message = reason[:500]
            db.commit()
            logger.error("Document id=%d marked failed: %s", document_id, reason)
    except Exception as e:
        logger.error("Could not mark document %d as failed: %s", document_id, e)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
#  Main entry point  (called by FastAPI background task)
# ─────────────────────────────────────────────────────────────

async def process_document(document_id: int) -> None:
    """
    Full extraction pipeline for one document.
    Called as:  await process_document(document_id)
    Never raises — all errors are caught and written to DB.
    """
    logger.info("═══ Pipeline START  document id=%d ═══", document_id)

    # ── 1. Load document record ──────────────────────────────
    db = _get_session()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Document id=%d not found in DB", document_id)
            return

        file_path  = _get_file_path(doc)
        page_count = getattr(doc, "page_count", None) or 1
        char_count = getattr(doc, "char_count",  None) or 0

        # Mark processing immediately so the UI shows correct state
        doc.status = "processing"
        db.commit()
        logger.info(
            "Document id=%d  file=%s  pages=%d  chars=%d",
            document_id, file_path, page_count, char_count,
        )
    except Exception as e:
        logger.error("DB read error for document id=%d: %s", document_id, e)
        logger.error(traceback.format_exc())
        return
    finally:
        db.close()

    # ── 2. Read PDF bytes ────────────────────────────────────
    if not file_path:
        _mark_failed(document_id, "No file path stored on document record")
        return

    # Render stores uploads relative to /app (WORKDIR in Dockerfile)
    resolved = Path(file_path)
    if not resolved.is_absolute():
        resolved = Path("/app") / resolved

    if not resolved.exists():
        # Try UPLOAD_DIR prefix from env
        upload_dir = os.getenv("UPLOAD_DIR", "data/raw")
        resolved = Path("/app") / upload_dir / Path(file_path).name

    if not resolved.exists():
        _mark_failed(document_id, f"PDF file not found at {file_path}")
        return

    try:
        pdf_bytes = resolved.read_bytes()
        logger.info("Read %d bytes from %s", len(pdf_bytes), resolved)
    except Exception as e:
        _mark_failed(document_id, f"Cannot read PDF: {e}")
        return

    # ── 3. Primary extraction  (Gemini Flash, free) ──────────
    final_result = None
    used_fallback = False

    try:
        logger.info("Running primary extraction (Gemini Flash)…")
        primary = extract_primary(
            pdf_bytes=pdf_bytes,
            page_count=page_count,
            char_count=char_count,
        )
        logger.info(
            "Primary done — confidence=%.2f  vendor=%s  invoice=%s  total=%s",
            (primary or {}).get("ai_confidence", 0),
            (primary or {}).get("vendor_name"),
            (primary or {}).get("invoice_number"),
            (primary or {}).get("total_amount"),
        )

        # ── 4. Fallback if needed  (Gemini Pro, free) ────────
        if needs_fallback(primary, threshold=CONFIDENCE_THRESHOLD):
            used_fallback = True
            logger.info("Triggering fallback extraction (Gemini Pro)…")
            fallback = extract_fallback(
                pdf_bytes=pdf_bytes,
                page_count=page_count,
                char_count=char_count,
            )
            logger.info(
                "Fallback done — confidence=%.2f",
                (fallback or {}).get("ai_confidence", 0),
            )

            if fallback and primary:
                final_result = merge_best(primary, fallback)
            elif fallback:
                final_result = fallback
            else:
                final_result = primary   # fallback returned nothing, use primary
        else:
            final_result = primary

    except Exception as e:
        logger.error("Extraction error for document id=%d:", document_id)
        logger.error(traceback.format_exc())
        _mark_failed(document_id, f"Extraction error: {str(e)[:400]}")
        return

    # ── 5. Determine status ──────────────────────────────────
    if final_result is None:
        _mark_failed(document_id, "Both primary and fallback returned no result")
        return

    confidence = final_result.get("ai_confidence", 0.0)
    status = "completed" if confidence >= 0.50 else "needs_review"

    logger.info(
        "Final result — confidence=%.2f  status=%s  fallback_used=%s",
        confidence, status, used_fallback,
    )

    # ── 6. Write to DB ───────────────────────────────────────
    db = _get_session()
    try:
        _update_document(db, document_id, final_result, status)
        _write_fields(db, document_id, final_result)
    except Exception as e:
        logger.error("DB write error for document id=%d: %s", document_id, e)
        logger.error(traceback.format_exc())
        _mark_failed(document_id, f"DB write error: {str(e)[:400]}")
        return
    finally:
        db.close()

    logger.info("═══ Pipeline END  document id=%d  status=%s ═══", document_id, status)