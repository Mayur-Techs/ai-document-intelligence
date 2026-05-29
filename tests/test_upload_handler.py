from __future__ import annotations

from pathlib import Path

import pytest

from upload.handler import ValidationResult, save_upload


class FakeUploadFile:
    def __init__(self, content: bytes):
        self._content = content

    async def read(self) -> bytes:
        return self._content


def _valid_pdf(name: str = "invoice.pdf", size: int = 14) -> ValidationResult:
    return ValidationResult(
        valid=True,
        error=None,
        original_name=name,
        mime_type="application/pdf",
        size_bytes=size,
    )


@pytest.mark.asyncio
async def test_save_upload_returns_s3_uri_when_s3_upload_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr("upload.handler.is_s3_enabled", lambda: True)
    monkeypatch.setattr(
        "upload.handler.upload_file_bytes",
        lambda content, key, content_type: f"s3://doc-intel-uploads/{key}",
    )

    saved = await save_upload(FakeUploadFile(b"%PDF-1.4 test"), _valid_pdf())

    assert str(saved.path).startswith("s3://doc-intel-uploads/")
    assert saved.size_bytes == len(b"%PDF-1.4 test")
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_save_upload_falls_back_to_local_disk_when_s3_upload_fails(monkeypatch, tmp_path):
    def raise_s3_error(content, key, content_type):
        raise OSError("AccessDenied")

    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr("upload.handler.is_s3_enabled", lambda: True)
    monkeypatch.setattr("upload.handler.upload_file_bytes", raise_s3_error)

    saved = await save_upload(FakeUploadFile(b"%PDF-1.4 test"), _valid_pdf())

    saved_path = Path(saved.path)
    assert saved_path.parent == tmp_path
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"%PDF-1.4 test"
