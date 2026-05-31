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

import asyncio
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

CONFIDENCE_THRESHOLD = 0.90
CONSECUTIVE_TIER1_FAILURES = 0

GROQ_LOCK = asyncio.Lock()
MISTRAL_LOCK = asyncio.Lock()
GEMINI_LOCK = asyncio.Lock()
EXTRACTION_SEMAPHORE = asyncio.Semaphore(3)


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
    async with EXTRACTION_SEMAPHORE:
        await _process_document_impl(document_id)


async def _process_document_impl(document_id: int) -> None:
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

    # 3. Extraction waterfall: Groq → Mistral → Gemini circular rotation with locks
    providers = [
        {"name": "groq", "func": extract_groq},
        {"name": "mistral", "func": extract_mistral},
        {"name": "gemini", "func": extract_gemini}
    ]

    attempt = 0
    provider_index = 0
    max_attempts = 5
    candidates = []
    groq_called_in_task = False
    groq_succeeded_in_task = False

    global CONSECUTIVE_TIER1_FAILURES
    if CONSECUTIVE_TIER1_FAILURES > 0:
        logger.info("Consecutive Tier 1 failures detected (%d). Sleeping 1s for quota reset...", CONSECUTIVE_TIER1_FAILURES)
        await asyncio.sleep(1)

    try:
        while attempt < max_attempts:
            current = providers[provider_index % len(providers)]
            provider_name = current["name"]
            func = current["func"]

            if provider_name == "groq":
                lock = GROQ_LOCK
            elif provider_name == "mistral":
                lock = MISTRAL_LOCK
            else:
                lock = GEMINI_LOCK

            # If provider is busy, skip to next provider in the cycle immediately
            if lock.locked():
                logger.info("Provider %s is busy, skipping to next provider", provider_name.upper())
                provider_index += 1
                # If all are busy, sleep a bit to avoid CPU spin
                all_busy = GROQ_LOCK.locked() and MISTRAL_LOCK.locked() and GEMINI_LOCK.locked()
                if all_busy:
                    logger.info("All providers are busy. Sleeping 0.5s...")
                    await asyncio.sleep(0.5)
                continue

            # Execute extraction under provider lock
            if provider_name == "groq":
                groq_called_in_task = True
            await lock.acquire()
            logger.info("Attempt %d: Extracting via %s", attempt + 1, provider_name.upper())

            should_break = False
            try:
                res = func(pdf_bytes, page_count, char_count)
                if res:
                    res = verify_extraction(res)
                    conf = res.get("ai_confidence", 0.0)
                    logger.info(
                        "%s done — conf=%.2f  vendor=%s  invoice=%s  total=%s",
                        provider_name.upper(),
                        conf,
                        res.get("vendor_name"),
                        res.get("invoice_number"),
                        res.get("total_amount"),
                    )

                    if conf > 0.0:
                        candidates.append((res, provider_name))
                        if provider_name == "groq":
                            groq_succeeded_in_task = True
                        # Check confidence threshold and missing critical fields
                        if not needs_fallback(res, threshold=CONFIDENCE_THRESHOLD):
                            logger.info("Success with %s! Threshold met (conf=%.2f >= %.2f)", provider_name.upper(), conf, CONFIDENCE_THRESHOLD)
                            should_break = True
                        else:
                            logger.info("%s succeeded but confidence %.2f < %.2f or fields missing.", provider_name.upper(), conf, CONFIDENCE_THRESHOLD)
                    else:
                        logger.warning("%s returned 0.00 confidence.", provider_name.upper())
                else:
                    logger.warning("%s returned None.", provider_name.upper())
            except Exception as e:
                logger.error("%s layer failed with error: %s", provider_name.upper(), e)
            finally:
                lock.release()

            # Consecutive failure logic moved to the end of the waterfall loop

            if should_break:
                break

            # Advance sequence
            attempt += 1
            provider_index += 1

            # Exponential backoff if full provider cycle wraps around
            if provider_index % len(providers) == 0 and attempt < max_attempts:
                logger.info("Full provider cycle exhausted. Cooling down for 2 seconds...")
                await asyncio.sleep(2)

        # Track Tier 1 consecutive failures on Groq attempts across document tasks
        if groq_called_in_task:
            if groq_succeeded_in_task:
                CONSECUTIVE_TIER1_FAILURES = 0
            else:
                CONSECUTIVE_TIER1_FAILURES += 1

        # Consolidate candidate results to choose the best successful historical data
        final_result = None
        provider_used = "none"

        if candidates:
            # Use the one with the highest confidence
            best_candidate, provider_used = max(candidates, key=lambda c: c[0].get("ai_confidence", 0.0))
            final_result = best_candidate

            # Merge improvements from other successful candidates
            for other, _ in candidates:
                if other is not final_result:
                    final_result = merge_best(final_result, other)
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
