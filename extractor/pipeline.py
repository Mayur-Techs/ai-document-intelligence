"""
extractor/pipeline.py — Orchestrates the full document processing pipeline.

Called by the FastAPI background task after a file is uploaded.

Pipeline steps:
  1. Update document status → PROCESSING
  2. Extract raw text from PDF (parser/extractor.py)
  3. Send text to Claude Sonnet (processor/llm.py) → structured JSON
  4. Validate JSON with Pydantic
  5. Write extracted fields to DB (documents + extracted_fields tables)
  6. Update document status → COMPLETED
  7. On any failure → status = FAILED, error_message logged + stored

WHY one orchestrator module?
Each step is independently testable. The orchestrator just wires them together.
Same separation as System 1's main.py pipeline runner.

WHY not put this in api/routes?
Routes handle HTTP. Pipelines handle business logic. Mixing them creates
untestable routes — you can't call a route without spinning up the HTTP layer.
This pipeline function can be called from tests, CLI, or routes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.connection import SessionLocal
from database.models import Document, ExtractedField, ProcessingStatus
from parser.extractor import ExtractionResult, extract_text
from processor.llm import ExtractionOutput, extract_fields

logger = logging.getLogger("docai.extractor.pipeline")


async def process_document(document_id: int) -> None:
    """
    Full extraction pipeline for a single document.
    Designed to run as a FastAPI BackgroundTask.

    Args:
        document_id: DB primary key of the Document row (already created on upload).

    Side effects:
        - Updates Document.status throughout processing
        - Writes ExtractedField rows on success
        - Writes Document.error_message on failure
        - Never raises — all exceptions are caught, logged, and stored in DB
    """
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Document %d not found — cannot process", document_id)
            return

        # ── Step 1: Mark as processing ─────────────────────────────────────────
        doc.status = ProcessingStatus.PROCESSING
        doc.processing_started_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Processing document %d: %s", document_id, doc.file_name)

        # ── Step 2: Extract raw text from PDF ──────────────────────────────────
        extraction: ExtractionResult = extract_text(doc.file_path)

        if not extraction.success or not extraction.text.strip():
            error_msg = extraction.error or "PDF text extraction returned empty result"
            _fail(db, doc, error_msg)
            return

        doc.raw_text = extraction.text
        doc.page_count = extraction.page_count
        doc.char_count = len(extraction.text)
        db.commit()
        logger.info(
            "Extracted %d chars from %d pages (document %d)",
            doc.char_count, doc.page_count, document_id,
        )

        # ── Step 3: Send to Claude for structured extraction ───────────────────
        output: ExtractionOutput = await extract_fields(
            text=extraction.text,
            document_type=doc.document_type.value,
        )

        if not output.success:
            _fail(db, doc, output.error or "LLM extraction failed")
            return

        # ── Step 4: Write top-level summary fields onto Document row ───────────
        _apply_summary_fields(doc, output)
        db.commit()

        # ── Step 5: Write individual fields to ExtractedField table ───────────
        _write_extracted_fields(db, doc, output)
        db.commit()

        # ── Step 6: Mark complete ──────────────────────────────────────────────
        doc.status = ProcessingStatus.COMPLETED
        doc.processing_completed_at = datetime.now(timezone.utc)
        db.commit()

        elapsed = (
            doc.processing_completed_at - doc.processing_started_at
        ).total_seconds()
        logger.info(
            "Document %d completed in %.1fs — vendor=%r total=%s confidence=%.0f%%",
            document_id,
            elapsed,
            doc.vendor_name,
            doc.total_amount,
            (doc.ai_confidence or 0) * 100,
        )

    except Exception as exc:
        logger.exception("Unhandled error processing document %d", document_id)
        try:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                _fail(db, doc, f"Unhandled exception: {exc}")
        except Exception:
            pass
    finally:
        db.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fail(db, doc: Document, error: str) -> None:
    """Mark document as FAILED with error message."""
    doc.status = ProcessingStatus.FAILED
    doc.error_message = error[:2000]  # truncate to column limit
    doc.processing_completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.error("Document %d FAILED: %s", doc.id, error)


def _apply_summary_fields(doc: Document, output: ExtractionOutput) -> None:
    """Copy top-level fields from ExtractionOutput onto the Document row."""
    data = output.data
    doc.vendor_name    = data.get("vendor_name") or data.get("vendor")
    doc.invoice_number = data.get("invoice_number") or data.get("invoice_no")
    doc.invoice_date   = data.get("invoice_date") or data.get("date")
    doc.due_date       = data.get("due_date") or data.get("payment_due")
    doc.ai_confidence  = output.confidence

    # Parse total_amount to float
    raw_total = data.get("total_amount") or data.get("total") or data.get("grand_total")
    if raw_total is not None:
        try:
            # Strip currency symbols, commas
            clean = str(raw_total).replace(",", "").replace("₹", "").replace("$", "").strip()
            doc.total_amount = float(clean)
        except (ValueError, TypeError):
            doc.total_amount = None

    doc.currency = data.get("currency", "INR")


def _write_extracted_fields(
    db, doc: Document, output: ExtractionOutput
) -> None:
    """
    Flatten ExtractionOutput.data into ExtractedField rows.

    Lists (like line_items) become multiple rows with enumerated keys:
      line_item_1, line_item_2, ...
    Nested dicts are JSON-serialized into field_value.
    """
    import json

    def write_field(name: str, value, field_type: str = "string") -> None:
        if value is None:
            return
        field = ExtractedField(
            document_id=doc.id,
            field_name=name,
            field_value=str(value) if not isinstance(value, (dict, list)) else json.dumps(value),
            field_type=field_type,
            confidence=output.confidence,
        )
        db.add(field)

    for key, value in output.data.items():
        if isinstance(value, list):
            for i, item in enumerate(value, 1):
                write_field(f"{key}_{i}", item, "list_item")
        elif isinstance(value, dict):
            write_field(key, value, "object")
        elif isinstance(value, (int, float)):
            write_field(key, value, "number")
        else:
            write_field(key, value, "string")
