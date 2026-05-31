"""
extractor/pipeline.py
──────────────────────
Entry point: await process_document(document_id)

Extraction waterfall (all free tiers, separate vendors/quotas):
  1. Groq        → llama-3.3-70b-versatile  (primary, fastest)
  2. Mistral     → mistral-small-latest      (if Groq fails or conf < threshold)
  3. Gemini      → gemini-1.5-flash          (final fallback, native PDF)

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
from extractor.field_sanitizer import merge_best, verify_extraction
from extractor.gemini_fallback_extractor import extract_gemini
from extractor.groq_extractor import extract_fallback as extract_groq
from extractor.groq_extractor import needs_fallback
from extractor.mistral_extractor import extract_mistral

logger = logging.getLogger("docai.pipeline")

CONFIDENCE_THRESHOLD = 0.99


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

        raw_file_path = doc.file_path
        is_s3 = raw_file_path and raw_file_path.startswith("s3://")

        # Resolve local path if it is not S3
        file_path = None if is_s3 else _resolve_path(doc)

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
    try:
        if is_s3:
            from utils.s3 import download_file_bytes

            # S3 URI is "s3://bucket-name/key"
            s3_path = raw_file_path[5:]  # Remove "s3://"
            parts = s3_path.split("/", 1)
            key = parts[1] if len(parts) == 2 else s3_path

            pdf_bytes = download_file_bytes(key)
            logger.info("Read %d bytes from S3 key: %s", len(pdf_bytes), key)
        else:
            if not file_path:
                _mark_failed(document_id, f"File not found on local disk: {raw_file_path}")
                return
            pdf_bytes = file_path.read_bytes()
            logger.info("Read %d bytes from local file %s", len(pdf_bytes), file_path)
    except Exception as e:
        _mark_failed(document_id, f"Cannot read PDF: {e}")
        return

    # 3. Extraction waterfall: Groq → Mistral → Gemini
    final_result = None
    provider_used = "none"

    try:
        # ── Tier 1: Groq (primary — fastest free tier) ──────────────────────
        logger.info("Tier 1 → Groq llama-3.3-70b-versatile")
        groq_result = extract_groq(pdf_bytes, page_count, char_count)
        if groq_result:
            groq_result = verify_extraction(groq_result)
        logger.info(
            "Groq done — conf=%.2f  vendor=%s  invoice=%s  total=%s",
            (groq_result or {}).get("ai_confidence", 0),
            (groq_result or {}).get("vendor_name"),
            (groq_result or {}).get("invoice_number"),
            (groq_result or {}).get("total_amount"),
        )

        if not needs_fallback(groq_result, threshold=CONFIDENCE_THRESHOLD):
            final_result = groq_result
            provider_used = "groq"
        else:
            # ── Tier 2: Mistral ─────────────────────────────────────────────
            logger.info("Tier 2 → Mistral mistral-small-latest")
            mistral_result = extract_mistral(pdf_bytes, page_count, char_count)
            if mistral_result:
                mistral_result = verify_extraction(mistral_result)
            logger.info(
                "Mistral done — conf=%.2f",
                (mistral_result or {}).get("ai_confidence", 0),
            )

            if not needs_fallback(mistral_result, threshold=CONFIDENCE_THRESHOLD):
                # Mistral succeeded — optionally merge with Groq partial result
                if groq_result and mistral_result:
                    final_result = merge_best(groq_result, mistral_result)
                else:
                    final_result = mistral_result or groq_result
                provider_used = "mistral"
            else:
                # ── Tier 3: Gemini (final fallback — native PDF understanding) ──
                logger.info("Tier 3 → Gemini gemini-1.5-flash (native PDF)")
                gemini_result = extract_gemini(pdf_bytes, page_count, char_count)
                if gemini_result:
                    gemini_result = verify_extraction(gemini_result)
                logger.info(
                    "Gemini done — conf=%.2f",
                    (gemini_result or {}).get("ai_confidence", 0),
                )

                # Pick the best result from all three
                candidates = [r for r in [groq_result, mistral_result, gemini_result] if r]
                if candidates:
                    # Use the one with highest confidence, then merge improvements
                    best = max(candidates, key=lambda r: r.get("ai_confidence", 0))
                    for other in candidates:
                        if other is not best:
                            best = merge_best(best, other)
                    final_result = best
                    provider_used = "gemini"
                else:
                    final_result = None

    except Exception as e:
        logger.error("Extraction error: %s\n%s", e, traceback.format_exc())
        _mark_failed(document_id, str(e)[:400])
        return

    if final_result is None:
        _mark_failed(document_id, "All providers (Groq, Mistral, Gemini) returned no result")
        return

    confidence = final_result.get("ai_confidence", 0.0)
    status = "completed" if confidence >= 0.50 else "needs_review"

    # 4. Write results to DB + update live counter
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
        "═══ Pipeline END  doc=%d  status=%s  provider=%s  conf=%.2f ═══",
        document_id,
        status,
        provider_used,
        confidence,
    )
