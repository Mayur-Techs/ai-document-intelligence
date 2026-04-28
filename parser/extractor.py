"""
parser/extractor.py — Extract raw text from PDF files.

WHY pdfplumber as primary, PyMuPDF as fallback?
pdfplumber preserves table structure better — critical for invoice line items.
It extracts text in reading order and handles multi-column layouts.
PyMuPDF (fitz) is faster and handles more PDF variants (including damaged files).
Using both with fallback gives best coverage across real-world documents.

WHY not just send the PDF to Claude directly?
Claude can accept PDF images but:
  1. It costs 3-5x more tokens than sending extracted text
  2. Text extraction gives us structured data we can store and search
  3. We can validate extraction quality before sending to LLM
  4. We maintain the raw text for debugging and re-processing

Pipeline:
  PDF file → extract_text() → raw string → processor/llm.py → structured JSON
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("docai.parser.extractor")

# Module-level imports — required for unittest.mock.patch to work.
# patch("parser.extractor.pdfplumber") only works if pdfplumber is a module-level name.
# Lazy imports inside functions cannot be patched from outside.
try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore[assignment]

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore[assignment]

# Max characters to send to Claude — Sonnet handles 200K context but we cap at
# 8000 for cost efficiency. A 10-page invoice rarely exceeds 6000 chars of useful text.
MAX_TEXT_CHARS = 8_000


@dataclass
class ExtractionResult:
    """Result of PDF text extraction. Passed directly to the LLM processor."""
    text: str               # extracted text, truncated to MAX_TEXT_CHARS
    page_count: int         # total pages in the PDF
    raw_length: int         # full text length before truncation
    method: str             # "pdfplumber" | "pymupdf" | "failed"
    truncated: bool         # True if text was cut at MAX_TEXT_CHARS
    # Derived convenience fields — consistent with System 1 pipeline pattern
    success: bool = True    # False if extraction failed entirely
    error: str | None = None  # set when success=False


def truncate_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """
    Truncate text to max_chars, appending a notice if truncated.
    Public function so tests and pipeline can use it independently.
    """
    if len(text) <= max_chars:
        return text
    cutoff = max_chars - 80  # leave room for truncation notice
    return text[:cutoff] + f"\n\n[truncated: original length {len(text)} chars, showing first {cutoff}]"


def extract_text(file_path: str | Path) -> ExtractionResult:
    """
    Extract text from a PDF. Tries pdfplumber first, falls back to PyMuPDF.

    Args:
        file_path: path to PDF file on disk

    Returns:
        ExtractionResult with text + metadata

    Never raises — returns ExtractionResult with method="failed" on error.
    Callers check result.method and route accordingly.
    """
    path = Path(file_path)

    if not path.exists():
        logger.error("File not found: %s", path)
        return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error=f"File not found: {path}")

    if path.suffix.lower() != ".pdf":
        logger.warning("Non-PDF file submitted: %s", path.suffix)
        return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error=f"Not a PDF: {path.suffix}")

    # Try pdfplumber first — better table extraction for invoices
    result = _extract_with_pdfplumber(path)
    if result.method != "failed" and len(result.text.strip()) > 50:
        return result

    # pdfplumber returned too little text (likely scanned PDF — no text layer)
    if result.method != "failed" and len(result.text.strip()) <= 50:
        logger.info("pdfplumber gave sparse text for %s — trying PyMuPDF", path.name)

    # Fallback to PyMuPDF — handles more PDF variants
    result = _extract_with_pymupdf(path)

    # Both extractors returned content but it's empty → scanned PDF
    if result.success and len(result.text.strip()) <= 50:
        return ExtractionResult(
            text="", page_count=result.page_count, raw_length=0,
            method="failed", truncated=False,
            success=False, error="no text layer found — PDF may be image-only (scanned)",
        )
    return result


def _extract_with_pdfplumber(path: Path) -> ExtractionResult:
    try:
        if pdfplumber is None:
            return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error="pdfplumber not installed")

        full_text_parts: list[str] = []
        page_count = 0

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                # Extract tables first (preserves structure better for invoices)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            row_text = " | ".join(str(cell or "").strip() for cell in row)
                            if row_text.strip(" |"):
                                full_text_parts.append(row_text)

                # Then remaining text
                page_text = page.extract_text(x_tolerance=2, y_tolerance=2)
                if page_text:
                    full_text_parts.append(page_text)

        raw = "\n".join(full_text_parts)
        truncated = len(raw) > MAX_TEXT_CHARS
        text = raw[:MAX_TEXT_CHARS] if truncated else raw

        logger.info(
            "pdfplumber: %s — %d pages, %d chars%s",
            path.name, page_count, len(raw),
            " (truncated)" if truncated else "",
        )
        return ExtractionResult(
            text=text, page_count=page_count,
            raw_length=len(raw), method="pdfplumber", truncated=truncated,
        )

    except Exception as exc:
        logger.warning("pdfplumber failed for %s: %s", path.name, exc)
        return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error=str(exc))


def _extract_with_pymupdf(path: Path) -> ExtractionResult:
    try:
        if fitz is None:
            return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error="PyMuPDF not installed")

        full_text_parts: list[str] = []
        page_count = 0

        with fitz.open(str(path)) as doc:
            page_count = len(doc)
            for page in doc:
                text = page.get_text("text")
                if text.strip():
                    full_text_parts.append(text)

        raw = "\n".join(full_text_parts)
        truncated = len(raw) > MAX_TEXT_CHARS
        text = raw[:MAX_TEXT_CHARS] if truncated else raw

        logger.info(
            "PyMuPDF: %s — %d pages, %d chars%s",
            path.name, page_count, len(raw),
            " (truncated)" if truncated else "",
        )
        return ExtractionResult(
            text=text, page_count=page_count,
            raw_length=len(raw), method="pymupdf", truncated=truncated,
        )

    except Exception as exc:
        logger.error("PyMuPDF also failed for %s: %s", path.name, exc)
        return ExtractionResult(text="", page_count=0, raw_length=0, method="failed", truncated=False, success=False, error=str(exc))
