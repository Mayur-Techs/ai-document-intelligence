"""
extractor/pipeline.py
──────────────────────
Entry point: await process_document(document_id)

Extraction waterfall (all free tiers, separate vendors/quotas):
  1. Groq        → llama-3.3-70b-versatile  (primary, fastest)
  2. Mistral     → mistral-small-latest      (if Groq fails or conf < threshold)
  3. Gemini      → gemini-2.0-flash          (final fallback)

Also updates platform_stats after every extraction for the live counter.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import queue
import threading
import traceback
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from database.connection import SessionLocal
from database.models import Document, ExtractedField
from extractor.field_sanitizer import verify_extraction
from extractor.gemini_fallback_extractor import extract_gemini
from extractor.groq_extractor import extract_primary as groq_extract
from extractor.mistral_extractor import extract_mistral as mistral_extract

logger = logging.getLogger("docai.pipeline")

CONFIDENCE_THRESHOLD = 0.90
CONSECUTIVE_TIER1_FAILURES = 0
_TIER1_FAILURE_LOCK = asyncio.Lock()

GROQ_LOCK = asyncio.Lock()
MISTRAL_LOCK = asyncio.Lock()
GEMINI_LOCK = asyncio.Lock()

# Thread-safe global queue for serial processing
DOCUMENT_QUEUE: queue.Queue[tuple[int, str]] = queue.Queue()
QUEUE_ACTIVE = False
_WORKER_THREAD: threading.Thread | None = None


def _session() -> Session:
    return SessionLocal()


def resolve_document_path(doc) -> Path | None:
    """Resolve the local filesystem path for a document record.

    Tries multiple candidate attributes and base directories so it works
    both locally (no /app prefix) and inside the Docker container.
    """
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


# Keep the private alias so any code that imported `_resolve_path` directly
# continues to work without a hard break while callers migrate.
_resolve_path = resolve_document_path


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
                field_value=_json.dumps(item, ensure_ascii=False),
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
    """Increment global counter and running confidence sum for the live stats widget.

    The platform_stats table DDL is handled by init_db() at startup.
    This function only performs the fast upsert — no DDL in the hot path.
    """
    try:
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


def _log_extraction_event(event: str, **kwargs) -> None:
    """Emit structured JSON log — queryable in any log aggregator."""
    logger.info(_json.dumps({"event": event, **kwargs}))


async def process_document(document_id: int) -> None:
    if os.getenv("TESTING") == "true" or not QUEUE_ACTIVE:
        # Run directly in tests and CLI so we don't have to manage background task workers in test suite/CLI
        await _process_document_impl(document_id)
    else:
        # Queue for single-worker serial processing in production
        db = _session()
        try:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if not doc:
                logger.warning(
                    "[QUEUE] Document %d not found in DB. Queueing with empty path fallback.",
                    document_id,
                )
                local_file_path = ""
            else:
                local_path = resolve_document_path(doc)
                local_file_path = str(local_path) if local_path else ""
            DOCUMENT_QUEUE.put_nowait((document_id, local_file_path))
            logger.info(
                "[QUEUE] Document %d queued for serial extraction. Path: %s",
                document_id,
                local_file_path,
            )
        finally:
            db.close()


def serial_worker() -> None:
    """Dedicated background thread worker that processes the queue sequentially."""
    logger.info("[QUEUE] Single-worker background thread queue active.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(document_worker())
    except Exception as e:
        logger.error("[QUEUE] Critical error in thread worker: %s", e)
    finally:
        loop.close()


def start_worker_thread() -> None:
    """Start the single-worker background queue thread."""
    global _WORKER_THREAD, QUEUE_ACTIVE
    QUEUE_ACTIVE = True
    _WORKER_THREAD = threading.Thread(target=serial_worker, name="docai-worker", daemon=True)
    _WORKER_THREAD.start()
    logger.info("[QUEUE] Started background worker thread.")


async def document_worker() -> None:
    """Dedicated background task runner that executes queued extractions one at a time."""
    logger.info("[QUEUE] Starting document_worker loop in event loop.")
    loop = asyncio.get_event_loop()
    while True:
        item = None
        try:
            # Pull from queue in a non-blocking way for the event loop
            if isinstance(DOCUMENT_QUEUE, asyncio.Queue):
                item = await DOCUMENT_QUEUE.get()
            else:
                item = await loop.run_in_executor(None, DOCUMENT_QUEUE.get)
            if item is None:
                # Sentinel to stop
                logger.info("[QUEUE] Sentinel received. Stopping worker.")
                DOCUMENT_QUEUE.task_done()
                break

            doc_id, local_file_path = item
            logger.info(
                "[QUEUE] Starting serial processing for doc id=%d from path=%s",
                doc_id,
                local_file_path,
            )

            try:
                # To support the existing test patching _process_document_impl,
                # we call it directly if testing or if path does not exist.
                if (
                    os.getenv("TESTING") == "true"
                    or not local_file_path
                    or not os.path.exists(local_file_path)
                ):
                    await _process_document_impl(doc_id)
                else:
                    # Read only ONE file from disk into memory at a time
                    with open(local_file_path, "rb") as f:
                        pdf_bytes = f.read()
                    await extract_document_with_rotation(doc_id, pdf_bytes)
            except Exception as e:
                logger.error("[QUEUE] Error processing doc id=%d: %s", doc_id, e)
                _mark_failed(doc_id, str(e))
            finally:
                # Add a file clean-up step inside a finally: block within the worker
                # to delete (os.remove) the local temporary file from "data/raw" once extraction completes or fails.
                if local_file_path and os.getenv("TESTING") != "true":
                    try:
                        if os.path.exists(local_file_path):
                            os.remove(local_file_path)
                            logger.info("[QUEUE] Cleaned up temporary file: %s", local_file_path)
                    except Exception as cleanup_err:
                        logger.warning(
                            "[QUEUE] Failed to delete file %s: %s", local_file_path, cleanup_err
                        )

                DOCUMENT_QUEUE.task_done()

        except asyncio.CancelledError:
            logger.info("[QUEUE] Document worker cancelled.")
            break
        except Exception as e:
            logger.error("[QUEUE] Critical loop error: %s", e)


async def _process_document_impl(document_id: int) -> None:
    global CONSECUTIVE_TIER1_FAILURES
    logger.info("═══ Pipeline START (legacy / test) doc id=%d ═══", document_id)

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
        file_path = None if is_s3 else resolve_document_path(doc)
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

    await extract_document_with_rotation(document_id, pdf_bytes)


async def extract_document_with_rotation(document_id: int, pdf_bytes: bytes) -> None:
    global CONSECUTIVE_TIER1_FAILURES
    logger.info("═══ Pipeline START (extract_document_with_rotation) doc id=%d ═══", document_id)

    # 1. Load document from DB to check status and page/char counts
    db = _session()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("Doc id=%d not found", document_id)
            return

        page_count = getattr(doc, "page_count", None) or 1
        char_count = getattr(doc, "char_count", None) or 0
        doc.status = "processing"
        db.commit()
    except Exception as e:
        logger.error("DB read error: %s", e)
        return
    finally:
        db.close()

    # 2. Quota-reset cooldown sleep if consecutive failures hit
    async with _TIER1_FAILURE_LOCK:
        failures_snapshot = CONSECUTIVE_TIER1_FAILURES
    if failures_snapshot > 0:
        logger.info(
            "Consecutive Tier 1 failures detected (%d). Sleeping 1s for quota reset...",
            failures_snapshot,
        )
        await asyncio.sleep(1)

    # 3. Extraction Waterfall with lock checks for concurrency isolation
    provider_name = "groq"
    fallback_used = False
    groq_called_in_task = False
    groq_succeeded_in_task = False
    result = None

    # Tier 1: Groq
    try:
        if GROQ_LOCK.locked():
            logger.info("Groq is busy, skipping to Mistral fallback")
        else:
            groq_called_in_task = True
            async with GROQ_LOCK:
                result = await groq_extract(pdf_bytes, page_count, char_count)
                if result is not None:
                    result = verify_extraction(result)
                    if result.get("ai_confidence", 0) > 0:
                        groq_succeeded_in_task = True
    except Exception as e:
        logger.error("Groq layer failed with error: %s", e)

    # Tier 2: Mistral fallback
    if result is None or result.get("ai_confidence", 0) < 0.95:
        try:
            if MISTRAL_LOCK.locked():
                logger.info("Mistral is busy, skipping")
            else:
                logger.info("Groq result below threshold or skipped — trying Mistral fallback")
                async with MISTRAL_LOCK:
                    mistral_result = await mistral_extract(pdf_bytes, page_count, char_count)
                    if mistral_result is not None:
                        mistral_result = verify_extraction(mistral_result)
                        # Use Mistral result if Groq completely failed, or if Mistral has higher confidence
                        if result is None or mistral_result.get("ai_confidence", 0) > result.get(
                            "ai_confidence", 0
                        ):
                            result = mistral_result
                            fallback_used = True
                            provider_name = "mistral"
        except Exception as e:
            logger.error("Mistral layer failed with error: %s", e)

    # Tier 3: Gemini final fallback
    if result is None or result.get("ai_confidence", 0) < 0.90:
        try:
            if GEMINI_LOCK.locked():
                logger.info("Gemini is busy, skipping")
            else:
                logger.info("Mistral result below threshold or skipped — trying Gemini fallback")
                async with GEMINI_LOCK:
                    gemini_result = await extract_gemini(pdf_bytes, page_count, char_count)
                    if gemini_result is not None:
                        gemini_result = verify_extraction(gemini_result)
                        if result is None or gemini_result.get("ai_confidence", 0) > result.get(
                            "ai_confidence", 0
                        ):
                            result = gemini_result
                            fallback_used = True
                            provider_name = "gemini"
        except Exception as e:
            logger.error("Gemini layer failed with error: %s", e)

    # Track Tier 1 consecutive failures on Groq attempts across document tasks
    if groq_called_in_task:
        async with _TIER1_FAILURE_LOCK:
            if groq_succeeded_in_task:
                CONSECUTIVE_TIER1_FAILURES = 0
            else:
                CONSECUTIVE_TIER1_FAILURES += 1

    if result is None:
        _mark_failed(document_id, "All providers (Groq, Mistral, Gemini) returned no result")
        return

    confidence = result.get("ai_confidence", 0.0)
    status = "completed" if confidence >= 0.50 else "needs_review"

    # 5. Write results to DB + update platform stats
    db = _session()
    try:
        _update_document(db, document_id, result, status)
        _write_fields(db, document_id, result)
        _update_platform_stats(db, confidence)

        # Reload doc to log with correct UUID/ID
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            _log_extraction_event(
                "extraction_complete",
                doc_id=str(doc.id),
                provider=provider_name,
                confidence=confidence,
                fallback_triggered=fallback_used,
                vendor=result.get("vendor_name"),
                invoice_number=result.get("invoice_number"),
            )
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
        provider_name,
        confidence,
    )
