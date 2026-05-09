"""
extractor/pipeline.py — Document processing pipeline.

NEW FLOW (replaces old direct-to-Claude approach):

  Step 1: Parse PDF text + tables (parser/extractor.py)
  Step 2: Rules extraction (processor/rules.py) — zero cost, instant
  Step 3: If confidence >= 0.70 → DONE. No AI called.
  Step 4: If confidence < 0.70 → Gemini Flash (free) fills MISSING FIELDS ONLY
  Step 5: Merge rules result + AI result → write to DB

WHY this order matters:
  A standard Indian GST invoice has GSTIN (15-char regex), invoice number
  (labelled field), total (labelled "Grand Total"), and date — all extractable
  by rules. AI is only needed for vendor name (unstructured header text) and
  line items (table structure varies). On a clean digital PDF, rules alone
  achieve 70-90% confidence. AI call rate in practice: ~30% of documents.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.connection import SessionLocal
from database.models import Document, ExtractedField, ProcessingStatus
from parser.extractor import ExtractionResult, extract_text, extract_tables
from processor.rules import (
    RULES_CONFIDENCE_THRESHOLD,
    RulesResult,
    extract_from_tables,
    extract_from_text,
)
from processor.llm import ExtractionOutput, extract_fields

logger = logging.getLogger("docai.extractor.pipeline")


async def process_document(document_id: int) -> None:
    """
    Full extraction pipeline. Called as FastAPI BackgroundTask.
    Never raises — all failures stored in DB.
    """
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Document %d not found", document_id)
            return

        # ── Step 1: Mark processing ────────────────────────────────────────────
        doc.status = ProcessingStatus.PROCESSING.value
        doc.processing_started_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Processing document %d: %s", document_id, doc.file_name)

        # ── Step 2: Parse PDF ──────────────────────────────────────────────────
        extraction: ExtractionResult = extract_text(doc.file_path)
        if not extraction.success or not extraction.text.strip():
            _fail(db, doc, extraction.error or "PDF text extraction returned empty")
            return

        doc.raw_text = extraction.text
        doc.page_count = extraction.page_count
        doc.char_count = len(extraction.text)
        db.commit()

        # ── Step 3: Extract tables (best structured data) ──────────────────────
        tables = extract_tables(doc.file_path)
        table_data = extract_from_tables(tables) if tables else {}
        logger.info("Tables found: %d, fields from tables: %d",
                    len(tables), len(table_data))

        # ── Step 4: Rules extraction on raw text ───────────────────────────────
        rules: RulesResult = extract_from_text(extraction.text)

        # Merge table data into rules data (tables win for amounts/line_items)
        merged_data = {**rules.data, **table_data}
        rules.data = merged_data

        # Recalculate missing fields after table merge
        all_fields = ["invoice_number", "invoice_date", "due_date", "vendor_name",
                      "buyer_name", "total_amount", "subtotal", "tax_amount",
                      "currency", "vendor_gstin", "buyer_gstin", "line_items"]
        rules.missing_fields = [f for f in all_fields if f not in rules.data]

        # Recalculate confidence after merge
        key_fields = ["invoice_number", "invoice_date", "vendor_name", "total_amount", "tax_amount"]
        bonus_fields = ["vendor_gstin", "buyer_name", "subtotal", "due_date", "currency"]
        key_score = sum(1 for f in key_fields if f in rules.data) / len(key_fields)
        bonus_score = sum(1 for f in bonus_fields if f in rules.data) / len(bonus_fields)
        rules.confidence = round(key_score * 0.75 + bonus_score * 0.25, 3)

        logger.info(
            "Rules extraction: confidence=%.0f%%, fields=%d, missing=%s",
            rules.confidence * 100, len(rules.data), rules.missing_fields,
        )

        # ── Step 5: AI fallback — ONLY if rules confidence is low ─────────────
        final_data = dict(rules.data)
        ai_confidence: float | None = None
        ai_tokens = 0

        if rules.confidence >= RULES_CONFIDENCE_THRESHOLD:
            # Rules extracted enough — skip AI entirely
            logger.info(
                "Rules confidence %.0f%% >= threshold %.0f%% — AI skipped, saving tokens",
                rules.confidence * 100, RULES_CONFIDENCE_THRESHOLD * 100,
            )
            ai_confidence = rules.confidence

        else:
            # Ask AI only about the missing fields
            logger.info(
                "Rules confidence %.0f%% < threshold — calling Gemini for: %s",
                rules.confidence * 100, rules.missing_fields,
            )
            ai_output: ExtractionOutput = await extract_fields(
                text=extraction.text,
                document_type=doc.document_type,
                missing_fields=rules.missing_fields if rules.missing_fields else None,
            )
            ai_tokens = ai_output.tokens_used

            if ai_output.success:
                # Merge: rules data takes priority (it's more reliable for structured fields)
                # AI fills in the gaps (vendor name, addresses, complex line items)
                for key, value in ai_output.data.items():
                    if key not in final_data or final_data[key] is None:
                        final_data[key] = value
                ai_confidence = ai_output.confidence
            else:
                logger.warning("AI extraction failed: %s — using rules result only", ai_output.error)
                ai_confidence = rules.confidence

        # ── Step 6: Write summary fields to Document row ───────────────────────
        _apply_fields(doc, final_data, ai_confidence)
        db.commit()

        # ── Step 7: Write all fields to ExtractedField table ──────────────────
        _write_fields(db, doc, final_data, ai_confidence)
        db.commit()

        # ── Step 8: Mark complete ──────────────────────────────────────────────
        doc.status = ProcessingStatus.COMPLETED.value
        doc.processing_completed_at = datetime.now(timezone.utc)
        db.commit()

        elapsed = (doc.processing_completed_at - doc.processing_started_at).total_seconds()
        logger.info(
            "Document %d done in %.1fs | vendor=%r total=%s confidence=%.0f%% | "
            "rules=%d fields, AI tokens=%d",
            document_id, elapsed, doc.vendor_name, doc.total_amount,
            (doc.ai_confidence or 0) * 100, len(rules.data), ai_tokens,
        )

    except Exception as exc:
        logger.exception("Unhandled error in pipeline for document %d", document_id)
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
    doc.status = ProcessingStatus.FAILED.value
    doc.error_message = error[:2000]
    doc.processing_completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.error("Document %d FAILED: %s", doc.id, error)


def _apply_fields(doc: Document, data: dict, confidence: float | None) -> None:
    doc.vendor_name    = data.get("vendor_name")
    doc.invoice_number = data.get("invoice_number")
    doc.invoice_date   = data.get("invoice_date")
    doc.due_date       = data.get("due_date")
    doc.currency       = data.get("currency", "INR")
    doc.ai_confidence  = confidence

    raw_total = data.get("total_amount")
    if raw_total is not None:
        try:
            doc.total_amount = float(str(raw_total).replace(",", "").replace("₹", "").strip())
        except (ValueError, TypeError):
            doc.total_amount = None


def _write_fields(db, doc: Document, data: dict, confidence: float | None) -> None:
    import json

    def add(name: str, value, ftype: str = "string") -> None:
        if value is None:
            return
        db.add(ExtractedField(
            document_id=doc.id,
            field_name=name,
            field_value=str(value) if not isinstance(value, (dict, list)) else json.dumps(value),
            field_type=ftype,
            confidence=confidence,
        ))

    for key, value in data.items():
        if isinstance(value, list):
            for i, item in enumerate(value, 1):
                add(f"{key}_{i}", item, "list_item")
        elif isinstance(value, dict):
            add(key, value, "object")
        elif isinstance(value, (int, float)):
            add(key, value, "number")
        else:
            add(key, value, "string")
