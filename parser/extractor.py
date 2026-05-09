"""
parser/extractor.py — Extract raw text AND tables from PDF files.

Two extractors, in order:
  1. pdfplumber — better table structure, preserves invoice layout
  2. PyMuPDF (fitz) — faster, handles damaged/unusual PDFs

New in this version:
  extract_tables() — returns raw table data for processor/rules.py
  to parse line items directly from table structure before any AI call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("docai.parser.extractor")

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore[assignment]

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore[assignment]

MAX_TEXT_CHARS = 8_000


@dataclass
class ExtractionResult:
    text: str
    page_count: int
    raw_length: int
    method: str
    truncated: bool
    success: bool = True
    error: str | None = None


def truncate_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    cutoff = max_chars - 80
    return text[:cutoff] + f"\n\n[truncated: original length {len(text)} chars, showing first {cutoff}]"


def extract_text(file_path: str | Path) -> ExtractionResult:
    """
    Extract text from PDF. pdfplumber first, PyMuPDF fallback.
    Returns ExtractionResult. Never raises.
    """
    path = Path(file_path)

    if not path.exists():
        return ExtractionResult(text="", page_count=0, raw_length=0,
                                method="failed", truncated=False, success=False,
                                error=f"File not found: {path}")

    if path.suffix.lower() != ".pdf":
        return ExtractionResult(text="", page_count=0, raw_length=0,
                                method="failed", truncated=False, success=False,
                                error=f"Not a PDF: {path.suffix}")

    result = _extract_with_pdfplumber(path)
    if result.method != "failed" and len(result.text.strip()) > 50:
        return result

    if result.method != "failed" and len(result.text.strip()) <= 50:
        logger.info("pdfplumber gave sparse text — trying PyMuPDF")

    result = _extract_with_pymupdf(path)

    if result.success and len(result.text.strip()) <= 50:
        return ExtractionResult(
            text="", page_count=result.page_count, raw_length=0,
            method="failed", truncated=False, success=False,
            error="no text layer found — PDF may be image-only (scanned)",
        )
    return result


def extract_tables(file_path: str | Path) -> list[list[list[str]]]:
    """
    Extract all tables from PDF as a list of tables.
    Each table = list of rows. Each row = list of cell strings.

    Used by processor/rules.py to extract line items directly from
    table structure — much more reliable than regex on unstructured text.

    Returns empty list if pdfplumber not available or no tables found.
    Never raises.
    """
    path = Path(file_path)
    if pdfplumber is None or not path.exists():
        return []

    try:
        all_tables: list[list[list[str]]] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if table:
                        # Normalize: convert None cells to empty string
                        normalized = [
                            [str(cell).strip() if cell is not None else "" for cell in row]
                            for row in table
                            if any(cell for cell in row)  # skip fully empty rows
                        ]
                        if len(normalized) > 1:  # at least header + one data row
                            all_tables.append(normalized)

        logger.info("Tables extracted from %s: %d tables", path.name, len(all_tables))
        return all_tables

    except Exception as exc:
        logger.warning("Table extraction failed for %s: %s", path.name, exc)
        return []


def _extract_with_pdfplumber(path: Path) -> ExtractionResult:
    try:
        if pdfplumber is None:
            return ExtractionResult(text="", page_count=0, raw_length=0,
                                    method="failed", truncated=False, success=False,
                                    error="pdfplumber not installed")

        full_text_parts: list[str] = []
        page_count = 0

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            row_text = " | ".join(str(c or "").strip() for c in row)
                            if row_text.strip(" |"):
                                full_text_parts.append(row_text)

                page_text = page.extract_text(x_tolerance=2, y_tolerance=2)
                if page_text:
                    full_text_parts.append(page_text)

        raw = "\n".join(full_text_parts)
        truncated = len(raw) > MAX_TEXT_CHARS
        text = raw[:MAX_TEXT_CHARS] if truncated else raw
        return ExtractionResult(text=text, page_count=page_count,
                                raw_length=len(raw), method="pdfplumber", truncated=truncated)

    except Exception as exc:
        logger.warning("pdfplumber failed for %s: %s", path.name, exc)
        return ExtractionResult(text="", page_count=0, raw_length=0,
                                method="failed", truncated=False, success=False, error=str(exc))


def _extract_with_pymupdf(path: Path) -> ExtractionResult:
    try:
        if fitz is None:
            return ExtractionResult(text="", page_count=0, raw_length=0,
                                    method="failed", truncated=False, success=False,
                                    error="PyMuPDF not installed")

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
        return ExtractionResult(text=text, page_count=page_count,
                                raw_length=len(raw), method="pymupdf", truncated=truncated)

    except Exception as exc:
        logger.error("PyMuPDF also failed for %s: %s", path.name, exc)
        return ExtractionResult(text="", page_count=0, raw_length=0,
                                method="failed", truncated=False, success=False, error=str(exc))
