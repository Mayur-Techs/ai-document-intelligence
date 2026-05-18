"""
smart_pipeline.py
──────────────────
Main extraction orchestrator. Drop-in replacement for the old processing service.

Flow:
  1. Primary extraction  → gemini-2.0-flash (free, fast)
  2. Confidence check    → if < 0.80 OR any critical field is null
  3. Fallback extraction → gemini-1.5-pro (free, thorough)
  4. Merge best results  → primary fields + fallback fills the gaps
  5. Final sanitization  → already done inside extractor
  6. DB write            → update document + fields tables

Usage (inside your existing document processor):
    from smart_pipeline import process_document
    result = process_document(document_id=42, pdf_bytes=bytes_data)
"""

import logging
import traceback
from typing import Optional

from gemini_extractor import extract_primary, extract_fallback, needs_fallback
from field_sanitizer import merge_best

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.80


# ──────────────────────────────────────────────
#  DB helpers (adapt to your ORM/DB layer)
# ──────────────────────────────────────────────

def _update_document(db_session, document_id: int, result: dict, status: str) -> None:
    """Update the documents table with extraction results."""
    from database.models import Document  # adjust import to your project structure
    doc = db_session.query(Document).filter(Document.id == document_id).first()
    if not doc:
        return

    doc.status               = status
    doc.vendor_name          = result.get('vendor_name')
    doc.invoice_number       = result.get('invoice_number')
    doc.invoice_date         = result.get('invoice_date')
    doc.due_date             = result.get('due_date')
    doc.total_amount         = result.get('total_amount')
    doc.currency             = result.get('currency') or 'INR'
    doc.ai_confidence        = result.get('ai_confidence', 0)
    doc.error_message        = None
    db_session.commit()


def _write_fields(db_session, document_id: int, result: dict) -> None:
    """
    Write all extracted fields into the document_fields table.
    Deletes old fields first so re-processing is always clean.
    """
    from database.models import ExtractedField  # adjust import

    # Clear old fields
    db_session.query(DocumentField).filter(
        DocumentField.document_id == document_id
    ).delete()

    scalar_fields = [
        ('vendor_name',          result.get('vendor_name'),          'string'),
        ('vendor_gstin',         result.get('vendor_gstin'),         'string'),
        ('buyer_name',           result.get('buyer_name'),           'string'),
        ('buyer_gstin',          result.get('buyer_gstin'),          'string'),
        ('invoice_number',       result.get('invoice_number'),       'string'),
        ('invoice_date',         result.get('invoice_date'),         'string'),
        ('due_date',             result.get('due_date'),             'string'),
        ('currency',             result.get('currency'),             'string'),
        ('subtotal',             result.get('subtotal'),             'number'),
        ('tax_amount',           result.get('tax_amount'),           'number'),
        ('total_amount',         result.get('total_amount'),         'number'),
        ('bank_ifsc',            result.get('bank_ifsc'),            'string'),
        ('bank_account_number',  result.get('bank_account_number'),  'string'),
        ('bank_name',            result.get('bank_name'),            'string'),
    ]

    confidence = result.get('ai_confidence', 0.0)

    for field_name, field_value, field_type in scalar_fields:
        if field_value is None:
            continue
        field = DocumentField(
            document_id=document_id,
            field_name=field_name,
            field_value=str(field_value),
            field_type=field_type,
            confidence=confidence,
            is_verified=False,
        )
        db_session.add(field)

    # Line items
    for idx, item in enumerate(result.get('line_items', []), start=1):
        import json as _json
        field = DocumentField(
            document_id=document_id,
            field_name=f'line_items_{idx}',
            field_value=_json.dumps(item, ensure_ascii=False),
            field_type='list_item',
            confidence=confidence,
            is_verified=False,
        )
        db_session.add(field)

    db_session.commit()


# ──────────────────────────────────────────────
#  Main pipeline entry point
# ──────────────────────────────────────────────

def process_document(
    document_id: int,
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
    db_session=None,
) -> dict:
    """
    Full intelligent extraction pipeline for one document.

    Returns the final sanitized result dict regardless of whether DB is used.
    If db_session is None, DB writes are skipped (useful for testing).
    """
    logger.info(f"[Pipeline] Starting document {document_id} "
                f"({page_count}p, {char_count} chars)")

    final_result = None
    used_fallback = False

    try:
        # ── Stage 1: Primary extraction (Gemini Flash, free) ──────────────
        primary_result = extract_primary(
            pdf_bytes=pdf_bytes,
            page_count=page_count,
            char_count=char_count,
        )

        # ── Stage 2: Decide if fallback is needed ─────────────────────────
        if needs_fallback(primary_result, threshold=CONFIDENCE_THRESHOLD):
            used_fallback = True

            # ── Stage 3: Fallback extraction (Gemini Pro, free) ───────────
            fallback_result = extract_fallback(
                pdf_bytes=pdf_bytes,
                page_count=page_count,
                char_count=char_count,
            )

            if fallback_result and primary_result:
                # ── Stage 4: Merge — keep best fields from both ────────────
                final_result = merge_best(primary_result, fallback_result)
                logger.info(
                    f"[Pipeline] Merged results → confidence: "
                    f"{final_result.get('ai_confidence', 0):.2f}"
                )
            elif fallback_result:
                final_result = fallback_result
            else:
                # Both failed — use primary (even if low confidence)
                final_result = primary_result
        else:
            final_result = primary_result

        # ── Stage 5: Final status determination ───────────────────────────
        if final_result is None:
            status = 'failed'
            final_result = {
                'ai_confidence': 0.0,
                'error': 'Both primary and fallback extraction failed',
            }
        else:
            confidence = final_result.get('ai_confidence', 0)
            status = 'completed' if confidence >= 0.50 else 'needs_review'

        # ── Stage 6: DB write ──────────────────────────────────────────────
        if db_session is not None:
            _update_document(db_session, document_id, final_result, status)
            _write_fields(db_session, document_id, final_result)

        logger.info(
            f"[Pipeline] Document {document_id} → status={status}, "
            f"confidence={final_result.get('ai_confidence', 0):.2f}, "
            f"fallback_used={used_fallback}"
        )

        return {
            **final_result,
            'status': status,
            'fallback_used': used_fallback,
        }

    except Exception as e:
        logger.error(f"[Pipeline] Unhandled error for document {document_id}: {e}")
        logger.error(traceback.format_exc())

        if db_session is not None:
            try:
                from models import Document
                doc = db_session.query(Document).filter(Document.id == document_id).first()
                if doc:
                    doc.status = 'failed'
                    doc.error_message = str(e)[:500]
                    db_session.commit()
            except Exception:
                pass

        return {
            'status': 'failed',
            'error': str(e),
            'ai_confidence': 0.0,
            'fallback_used': used_fallback,
        }


# ──────────────────────────────────────────────
#  Standalone test (no DB needed)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python smart_pipeline.py path/to/invoice.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    result = process_document(
        document_id=0,
        pdf_bytes=pdf_bytes,
        db_session=None,   # skip DB in test mode
    )

    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))
