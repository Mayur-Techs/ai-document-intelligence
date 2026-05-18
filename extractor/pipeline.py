"""
extractor/pipeline.py
─────────────────────
The single entry point called by main.py and the API routes.

    from extractor.pipeline import process_document
    await process_document(document_id)

Flow:
    1. Load document record from DB → get file path, page count, char count
    2. Read PDF bytes from disk
    3. Run smart extraction pipeline:
         Primary  → Gemini 2.0 Flash  (free, fast)
         Fallback → Gemini 1.5 Pro    (free, deeper — auto-triggered if
                                       confidence < 0.80 or critical field is null)
    4. Sanitize every extracted field (dates, amounts, GSTINs, etc.)
    5. Write results back to DB:
         → documents table      (vendor_name, invoice_number, total_amount …)
         → extracted_fields table (one row per field + one row per line item)
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

from database.connection import get_db
from database.models import Document, ExtractedField
from extractor.gemini_extractor import extract_primary, extract_fallback, needs_fallback
from extractor.field_sanitizer import merge_best

logger = logging.getLogger("docai.pipeline")

CONFIDENCE_THRESHOLD = 0.80


# ─────────────────────────────────────────────────────────────
#  DB helpers
# ─────────────────────────────────────────────────────────────

def _update_document(db, document_id: int, result: dict, status: str) -> None:
    """Write top-level scalar fields back to the documents table."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        logger.error("Document id=%d not found in DB", document_id)
        return

    doc.status          = status
    doc.vendor_name     = result.get("vendor_name")
    doc.invoice_number  = result.get("invoice_number")
    doc.invoice_date    = result.get("invoice_date")
    doc.due_date        = result.get("due_date")
    doc.total_amount    = result.get("total_amount")
    doc.currency        = result.get("currency") or "INR"
    doc.ai_confidence   = result.get("ai_confidence", 0.0)
    doc.error_message   = None
    db.commit()
    logger.info(
        "Document id=%d updated → status=%s, confidence=%.2f",
        document_id, status, doc.ai_confidence or 0,
    )


def _write_fields(db, document_id: int, result: dict) -> None:
    """
    Write all extracted fields to extracted_fields table.
    Deletes old rows first so re-processing is always clean.
    """
    # Clear any previous extraction for this document
    db.query(ExtractedField).filter(
        ExtractedField.document_id == document_id
    ).delete(synchronize_session=False)

    confidence = result.get("ai_confidence", 0.0)

    # All scalar fields — only write if value is not None
    scalar_fields = [
        ("vendor_name",          result.get("vendor_name"),          "string"),
        ("vendor_gstin",         result.get("vendor_gstin"),         "string"),
        ("buyer_name",           result.get("buyer_name"),           "string"),
        ("buyer_gstin",          result.get("buyer_gstin"),          "string"),
        ("invoice_number",       result.get("invoice_number"),       "string"),
        ("invoice_date",         result.get("invoice_date"),         "string"),
        ("due_date",             result.get("due_date"),             "string"),
        ("currency",             result.get("currency"),             "string"),
        ("subtotal",             result.get("subtotal"),             "number"),
        ("tax_amount",           result.get("tax_amount"),           "number"),
        ("total_amount",         result.get("total_amount"),         "number"),
        ("bank_ifsc",            result.get("bank_ifsc"),            "string"),
        ("bank_account_number",  result.get("bank_account_number"),  "string"),
        ("bank_name",            result.get("bank_name"),            "string"),
    ]

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

    # Line items — one row each, value stored as JSON string
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
        "Wrote %d scalar fields + %d line items for document id=%d",
        sum(1 for _, v, _ in scalar_fields if v is not None),
        len(result.get("line_items", [])),
        document_id,
    )


# ─────────────────────────────────────────────────────────────
#  Main pipeline entry point  (called by main.py + API routes)
# ─────────────────────────────────────────────────────────────

async def process_document(document_id: int) -> None:
    """
    Full extraction pipeline for one document.

    Called as:  await process_document(document_id)

    Raises no exceptions — all errors are caught and written to DB.
    """
    logger.info("Pipeline started for document id=%d", document_id)

    # ── Load document from DB ────────────────────────────────
    with get_db() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Document id=%d not found — aborting", document_id)
            return

        file_path  = doc.file_path
        page_count = doc.page_count or 1
        char_count = doc.char_count or 0

        # Mark as processing immediately
        doc.status = "processing"
        db.commit()

    # ── Read PDF bytes from disk ─────────────────────────────
    try:
        pdf_bytes = Path(file_path).read_bytes()
    except (FileNotFoundError, PermissionError) as exc:
        logger.error("Cannot read PDF file %s: %s", file_path, exc)
        with get_db() as db:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "failed"
                doc.error_message = f"File not found: {file_path}"
                db.commit()
        return

    # ── Stage 1: Primary extraction (Gemini Flash, free) ─────
    final_result = None
    used_fallback = False

    try:
        primary_result = extract_primary(
            pdf_bytes=pdf_bytes,
            page_count=page_count,
            char_count=char_count,
        )
        logger.info(
            "Primary extraction done — confidence=%.2f",
            (primary_result or {}).get("ai_confidence", 0),
        )

        # ── Stage 2: Check if fallback needed ─────────────────
        if needs_fallback(primary_result, threshold=CONFIDENCE_THRESHOLD):
            used_fallback = True
            logger.info("Triggering fallback extraction (Gemini Pro)…")

            # ── Stage 3: Fallback (Gemini Pro, free) ──────────
            fallback_result = extract_fallback(
                pdf_bytes=pdf_bytes,
                page_count=page_count,
                char_count=char_count,
            )

            if fallback_result and primary_result:
                # Merge: primary fields are kept; fallback fills in nulls
                final_result = merge_best(primary_result, fallback_result)
                logger.info(
                    "Merged primary + fallback → confidence=%.2f",
                    final_result.get("ai_confidence", 0),
                )
            elif fallback_result:
                final_result = fallback_result
            else:
                # Both ran but fallback returned nothing — use primary anyway
                final_result = primary_result
        else:
            final_result = primary_result

    except Exception as exc:
        logger.error("Extraction error for document id=%d: %s", document_id, exc)
        logger.error(traceback.format_exc())
        with get_db() as db:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "failed"
                doc.error_message = str(exc)[:500]
                db.commit()
        return

    # ── Stage 4: Determine status ────────────────────────────
    if final_result is None:
        status = "failed"
        final_result = {"ai_confidence": 0.0}
    else:
        confidence = final_result.get("ai_confidence", 0.0)
        if confidence >= 0.80:
            status = "completed"
        elif confidence >= 0.50:
            # Passed fallback but some gaps remain — completed but flag it
            status = "completed"
        else:
            status = "needs_review"

    # ── Stage 5: Write everything to DB ─────────────────────
    try:
        with get_db() as db:
            _update_document(db, document_id, final_result, status)
            _write_fields(db, document_id, final_result)
    except Exception as exc:
        logger.error("DB write error for document id=%d: %s", document_id, exc)
        logger.error(traceback.format_exc())
        with get_db() as db:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "failed"
                doc.error_message = f"DB write error: {str(exc)[:400]}"
                db.commit()
        return

    logger.info(
        "Pipeline complete — document id=%d, status=%s, fallback_used=%s, confidence=%.2f",
        document_id, status, used_fallback, final_result.get("ai_confidence", 0),
    )