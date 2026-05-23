"""
main.py — AI Document Intelligence CLI.

Usage:
    python main.py --file invoice.pdf
    python main.py --file invoice.pdf --type contract
    python main.py --file invoice.pdf --dry-run       # extract text only, no Claude
    python main.py --dir ./invoices/                  # process entire directory

WHY a CLI separate from the API?
Same reason as System 1:
  - API serves HTTP clients (n8n, front-ends)
  - CLI runs batch jobs, dev testing, quick one-offs
  - Both use the same pipeline module — single source of truth
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from database.connection import get_db, init_db
from database.models import Document
from extractor.pipeline import process_document
from parser.extractor import extract_text
from utils.logger import setup_logging

logger = logging.getLogger("docai.cli")


async def process_file(file_path: str, doc_type: str, dry_run: bool) -> None:
    """Process a single PDF through the full pipeline."""

    path = Path(file_path)
    if not path.exists():
        logger.error("File not found: %s", file_path)
        return
    if path.suffix.lower() != ".pdf":
        logger.error("Not a PDF: %s", file_path)
        return

    logger.info("Processing: %s", path.name)

    if dry_run:
        # Step 1 only — extract and print text, no DB, no Claude
        result = extract_text(str(path))
        if result.success:
            logger.info(
                "Extracted %d chars from %d pages", result.page_count * 100, result.page_count
            )
            print("\n" + "─" * 60)
            print(result.text[:2000])
            if result.truncated:
                print(f"\n[... truncated — {result.raw_length} total chars]")
            print("─" * 60)
        else:
            logger.error("Extraction failed: %s", result.error)
        return

    # Full pipeline: save to DB → call process_document (extract + Claude + store)
    with get_db() as db:
        doc = Document(
            file_name=path.name,
            file_path=str(path.resolve()),
            file_size_bytes=path.stat().st_size,
            document_type=doc_type,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        doc_id = doc.id

    logger.info("Created document record id=%d", doc_id)
    await process_document(doc_id)

    # Print result
    with get_db() as db:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        print("\n" + "─" * 60)
        print(f"Status:    {doc.status}")
        print(f"Vendor:    {doc.vendor_name}")
        print(f"Invoice #: {doc.invoice_number}")
        print(f"Date:      {doc.invoice_date}")
        print(f"Total:     {doc.currency} {doc.total_amount}")
        print(f"Confidence: {int((doc.ai_confidence or 0) * 100)}%")
        if doc.error_message:
            print(f"Error:     {doc.error_message}")
        print("─" * 60)


async def process_directory(dir_path: str, doc_type: str, dry_run: bool) -> None:
    """Process all PDFs in a directory."""
    pdfs = list(Path(dir_path).glob("*.pdf"))
    if not pdfs:
        logger.warning("No PDF files found in %s", dir_path)
        return
    logger.info("Found %d PDFs in %s", len(pdfs), dir_path)
    for pdf in pdfs:
        await process_file(str(pdf), doc_type, dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Document Intelligence CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Path to a single PDF file")
    group.add_argument("--dir", help="Directory containing PDF files")
    parser.add_argument(
        "--type", default="invoice", choices=["invoice", "contract", "receipt", "report", "other"]
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Extract text only — no Claude API, no DB write"
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    setup_logging(level=args.log_level)

    if not args.dry_run:
        if not os.getenv("GEMINI_API_KEY"):
            logger.error("GEMINI_API_KEY not set. Get free key at aistudio.google.com")
            raise SystemExit(1)
        init_db()

    if args.file:
        asyncio.run(process_file(args.file, args.type, args.dry_run))
    else:
        asyncio.run(process_directory(args.dir, args.type, args.dry_run))
