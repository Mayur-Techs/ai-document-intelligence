"""
upload/handler.py — File validation and storage.

Handles the gap between "file received from HTTP" and "file ready for parsing".
Responsibilities:
  - Validate file type (PDF only for now)
  - Validate file size (max 50MB)
  - Save to disk with a UUID-based name (prevents collisions + path traversal)
  - Return the stored path for the pipeline to use

WHY UUID for filenames?
Original filenames are user-controlled: "../../etc/passwd.pdf" is a valid filename.
Storing the original name in the DB for display but saving with UUID prevents
path traversal attacks and name collisions entirely.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger("docai.upload.handler")

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/raw"))
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024
ALLOWED_MIME_TYPES = {"application/pdf"}
ALLOWED_EXTENSIONS = {".pdf"}


def ensure_upload_dir() -> None:
    """Create upload directory if it doesn't exist. Called on startup."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Upload directory: %s", UPLOAD_DIR.absolute())


def save_upload(file_bytes: bytes, original_filename: str) -> tuple[Path, str]:
    """
    Validate and save an uploaded file to disk.

    Args:
        file_bytes: raw file content
        original_filename: user-provided filename (used for display only)

    Returns:
        (stored_path, detected_mime_type) on success

    Raises:
        ValueError: invalid file type or size
        IOError: disk write failure
    """
    # Validate size first — cheap check before reading bytes
    size = len(file_bytes)
    if size == 0:
        raise ValueError("Empty file — nothing to process")
    if size > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"File too large: {size / 1024 / 1024:.1f}MB. "
            f"Maximum: {MAX_FILE_SIZE_BYTES // 1024 // 1024}MB"
        )

    # Validate extension
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: {ext!r}. "
            f"Supported: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # Validate by magic bytes — don't trust the extension alone
    mime = _detect_mime(file_bytes)
    if mime not in ALLOWED_MIME_TYPES:
        raise ValueError(
            f"File content is {mime!r}, not a valid PDF. "
            "Ensure the file is not corrupted."
        )

    # Save with UUID filename — prevents collisions and path traversal
    ensure_upload_dir()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = UPLOAD_DIR / stored_name

    stored_path.write_bytes(file_bytes)
    logger.info(
        "Saved upload: %s → %s (%d bytes)",
        original_filename, stored_path, size,
    )
    return stored_path, mime


def _detect_mime(data: bytes) -> str:
    """
    Detect file type from magic bytes — the first N bytes of the file.
    Magic bytes don't lie; file extensions do.

    PDF magic bytes: %PDF (hex 25 50 44 46)
    """
    if data[:4] == b"%PDF":
        return "application/pdf"
    # Extend here for other types (DOCX, XLSX, images) when System 2 grows
    return "application/octet-stream"


# --------------------------------------------------------------------------- #
# FastAPI-compatible async wrappers                                             #
# Routes import these — they accept UploadFile, not raw bytes                  #
# --------------------------------------------------------------------------- #

from dataclasses import dataclass


@dataclass
class ValidationResult:
    valid: bool
    error: str | None
    original_name: str
    mime_type: str
    size_bytes: int


@dataclass
class SavedFile:
    path: Path
    size_bytes: int


async def validate_file(upload_file) -> ValidationResult:
    """
    Validate a FastAPI UploadFile without saving it.
    Reads bytes to check magic bytes and size.
    Returns ValidationResult — never raises.
    """
    try:
        content = await upload_file.read()
        await upload_file.seek(0)  # reset for subsequent read by save_upload

        size = len(content)
        name = upload_file.filename or "unknown.pdf"
        ext = Path(name).suffix.lower()

        if size == 0:
            return ValidationResult(valid=False, error="Empty file", original_name=name, mime_type="", size_bytes=0)

        if size > MAX_FILE_SIZE_BYTES:
            return ValidationResult(
                valid=False,
                error=f"File too large: {size / 1024 / 1024:.1f}MB. Max: {MAX_FILE_SIZE_BYTES // 1024 // 1024}MB",
                original_name=name, mime_type="", size_bytes=size,
            )

        if ext not in ALLOWED_EXTENSIONS:
            mime_claim = upload_file.content_type or "unknown"
            return ValidationResult(
                valid=False,
                error=f"Only PDF files are accepted. Got: {mime_claim}",
                original_name=name, mime_type=mime_claim, size_bytes=size,
            )

        mime = _detect_mime(content)
        if mime not in ALLOWED_MIME_TYPES:
            return ValidationResult(
                valid=False, error=f"File content is not a valid PDF (got: {mime})",
                original_name=name, mime_type=mime, size_bytes=size,
            )

        return ValidationResult(valid=True, error=None, original_name=name, mime_type=mime, size_bytes=size)

    except Exception as exc:
        return ValidationResult(valid=False, error=str(exc), original_name="unknown", mime_type="", size_bytes=0)


async def save_upload(upload_file, validation: ValidationResult) -> SavedFile:
    """
    Save a validated UploadFile to disk. Returns SavedFile with path + size.
    Must be called after validate_file — assumes file pointer is at start.
    """
    content = await upload_file.read()
    ensure_upload_dir()

    ext = Path(validation.original_name).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = UPLOAD_DIR / stored_name
    stored_path.write_bytes(content)

    logger.info("Saved: %s → %s (%d bytes)", validation.original_name, stored_path, len(content))
    return SavedFile(path=stored_path, size_bytes=len(content))
