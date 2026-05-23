"""
extractor/pipeline.py
──────────────────────
Entry point: await process_document(document_id)

Primary  → Cerebras  (free, 1000+ tokens/sec, llama-3.3-70b)
Fallback → Groq      (free, separate vendor — triggers if confidence < 0.95
                       OR any critical field is null)

Also updates platform_stats after every extraction for the live counter.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Document, ExtractedField
from extractor.cerebras_extractor import extract_primary
from extractor.field_sanitizer import merge_best
from extractor.groq_extractor import extract_fallback, needs_fallback

logger = logging.getLogger("docai.pipeline")

CONFIDENCE_THRESHOLD = 0.95


def _session() -> Session:
    return SessionLocal()


def _resolve_path(doc) -> Path | None:
    for attr in ("file_path", "upload_path", "path", "file_name", "filename"):
        val = getattr(doc, attr, None)
        if not val:
            continue
        for base in ("", "/app", str(Path("/app") / os.getenv("UPLOAD_DIR", "data/raw"))):
            p = Path(base) / val if base else Path(val)
            if p.exists():
                return p
        # try just the filename under upload dir
        upload_dir = Path("/app") / os.getenv("UPLOAD_DIR", "data/raw")
        p = upload_dir / Path(val).name
        if p.exists():
            return p
    return None


def _update_document(db: Session, doc_id: int, result: dict, status: str) -> None:
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return
    doc.status = status
    doc.vendor_name = result.get("vendor_name")
    doc.invoice_number = result.get("invoice_number")
    doc.invoice_date = result.get("invoice_date")
    doc.due_date = result.get("due_date")
    doc.total_amount = result.get("total_amount")
    doc.currency = result.get("currency") or "INR"
    doc.ai_confidence = result.get("ai_confidence", 0.0)
    doc.error_message = None
    db.commit()
    logger.info("Doc id=%d → %s  conf=%.2f", doc_id, status, doc.ai_confidence or 0)


def _write_fields(db: Session, doc_id: int, result: dict) -> None:
    db.query(ExtractedField).filter(ExtractedField.document_id == doc_id).delete(
        synchronize_session=False
    )

    confidence = result.get("ai_confidence", 0.0)
    scalar_fields = [
        ("vendor_name", result.get("vendor_name"), "string"),
        ("vendor_gstin", result.get("vendor_gstin"), "string"),
        ("buyer_name", result.get("buyer_name"), "string"),
        ("buyer_gstin", result.get("buyer_gstin"), "string"),
        ("invoice_number", result.get("invoice_number"), "string"),
        ("invoice_date", result.get("invoice_date"), "string"),
        ("due_date", result.get("due_date"), "string"),
        ("currency", result.get("currency"), "string"),
        ("subtotal", result.get("subtotal"), "number"),
        ("tax_amount", result.get("tax_amount"), "number"),
        ("total_amount", result.get("total_amount"), "number"),
        ("bank_ifsc", result.get("bank_ifsc"), "string"),
        ("bank_account_number", result.get("bank_account_number"), "string"),
        ("bank_name", result.get("bank_name"), "string"),
    ]
    written = 0
    for name, value, ftype in scalar_fields:
        if value is None:
            continue
        db.add(
            ExtractedField(
                document_id=doc_id,
                field_name=name,
                field_value=str(value),
                field_type=ftype,
                confidence=confidence,
                is_verified=False,
            )
        )
        written += 1

    for idx, item in enumerate(result.get("line_items", []), start=1):
        db.add(
            ExtractedField(
                document_id=doc_id,
                field_name=f"line_items_{idx}",
                field_value=json.dumps(item, ensure_ascii=False),
                field_type="list_item",
                confidence=confidence,
                is_verified=False,
            )
        )

    db.commit()
    logger.info(
        "Wrote %d fields + %d line items for doc id=%d",
        written,
        len(result.get("line_items", [])),
        doc_id,
    )


def _update_platform_stats(db: Session, confidence: float) -> None:
    """Increment global counter and running confidence sum for the live stats widget."""
    try:
        db.execute(
            text("""
            CREATE TABLE IF NOT EXISTS platform_stats (
                id               INTEGER PRIMARY KEY DEFAULT 1,
                total_documents  INTEGER DEFAULT 0,
                confidence_sum   FLOAT   DEFAULT 0.0
            )
        """)
        )
        db.execute(
            text("""
            INSERT INTO platform_stats (id, total_documents, confidence_sum)
            VALUES (1, 1, :conf)
            ON CONFLICT (id) DO UPDATE
              SET total_documents = platform_stats.total_documents + 1,
                  confidence_sum  = platform_stats.confidence_sum  + :conf
        """),
            {"conf": float(confidence)},
        )
        db.commit()
    except Exception as e:
        logger.warning("platform_stats update failed (non-critical): %s", e)


def _mark_failed(doc_id: int, reason: str) -> None:
    db = _session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            doc.status = "failed"
            doc.error_message = reason[:500]
            db.commit()
            logger.error("Doc id=%d marked failed: %s", doc_id, reason)
    finally:
        db.close()


async def process_document(document_id: int) -> None:
    logger.info("═══ Pipeline START  doc id=%d ═══", document_id)

    # 1. Load document from DB
    db = _session()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Doc id=%d not found", document_id)
            return
        file_path = _resolve_path(doc)
        page_count = getattr(doc, "page_count", None) or 1
        char_count = getattr(doc, "char_count", None) or 0
        doc.status = "processing"
        db.commit()
    except Exception as e:
        logger.error("DB read error: %s", e)
        return
    finally:
        db.close()

    # 2. Read PDF bytes
    if not file_path:
        _mark_failed(document_id, "File not found on disk")
        return
    try:
        pdf_bytes = file_path.read_bytes()
        logger.info("Read %d bytes from %s", len(pdf_bytes), file_path)
    except Exception as e:
        _mark_failed(document_id, f"Cannot read PDF: {e}")
        return

    # 3. Primary extraction — Cerebras (free, fast)
    final_result = None
    used_fallback = False
    try:
        logger.info("Primary → Cerebras llama-3.3-70b")
        primary = extract_primary(pdf_bytes, page_count, char_count)
        logger.info(
            "Primary done — conf=%.2f  vendor=%s  invoice=%s  total=%s",
            (primary or {}).get("ai_confidence", 0),
            (primary or {}).get("vendor_name"),
            (primary or {}).get("invoice_number"),
            (primary or {}).get("total_amount"),
        )

        # 4. Fallback — Groq (completely separate vendor/quota)
        if needs_fallback(primary, threshold=CONFIDENCE_THRESHOLD):
            used_fallback = True
            logger.info("Fallback → Groq llama-3.3-70b-versatile")
            fallback = extract_fallback(pdf_bytes, page_count, char_count)
            logger.info(
                "Fallback done — conf=%.2f",
                (fallback or {}).get("ai_confidence", 0),
            )
            if fallback and primary:
                final_result = merge_best(primary, fallback)
            elif fallback:
                final_result = fallback
            else:
                final_result = primary
        else:
            final_result = primary

    except Exception as e:
        logger.error("Extraction error: %s\n%s", e, traceback.format_exc())
        _mark_failed(document_id, str(e)[:400])
        return

    if final_result is None:
        _mark_failed(document_id, "Both Cerebras and Groq returned no result")
        return

    confidence = final_result.get("ai_confidence", 0.0)
    status = "completed" if confidence >= 0.50 else "needs_review"

    # 5. Write results to DB + update live counter
    db = _session()
    try:
        _update_document(db, document_id, final_result, status)
        _write_fields(db, document_id, final_result)
        _update_platform_stats(db, confidence)
    except Exception as e:
        logger.error("DB write error: %s\n%s", e, traceback.format_exc())
        _mark_failed(document_id, str(e)[:400])
        return
    finally:
        db.close()

    logger.info(
        "═══ Pipeline END  doc=%d  status=%s  fallback=%s  conf=%.2f ═══",
        document_id,
        status,
        used_fallback,
        confidence,
    )
