"""
utils/email.py
───────────────
Single place for all outbound email sending.

Previously, auth/routes.py and api/export.py each contained their own
inline SMTP code — identical but copy-pasted, making fixes diverge.

Usage:
    from utils.email import send_email
    send_email(to="user@example.com", subject="...", body="...")

Returns True on success, False if SMTP is not configured or send fails.
Never raises — callers should treat email as best-effort.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("docai.email")


def _smtp_config() -> tuple[str | None, int, str | None, str | None]:
    """Read SMTP config from environment. Returns (host, port, user, password)."""
    host = os.getenv("SMTP_HOST")
    port_raw = os.getenv("SMTP_PORT", "587")
    port = int(port_raw) if port_raw and port_raw.isdigit() else 587
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    return host, port, user, password


def is_email_configured() -> bool:
    """Return True if all required SMTP env vars are set."""
    host, _, user, password = _smtp_config()
    return bool(host and user and password)


def send_email(
    to: str,
    subject: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
    attachment_content_type: str = "application/octet-stream",
) -> bool:
    """
    Send a plain-text email, optionally with a binary attachment.

    Args:
        to:                      recipient email address
        subject:                 email subject line
        body:                    plain-text email body
        attachment_bytes:        optional raw attachment bytes
        attachment_filename:     filename shown in email client
        attachment_content_type: MIME type for the attachment

    Returns:
        True  — email sent successfully
        False — SMTP not configured or send failed (error logged, never raised)
    """
    host, port, user, password = _smtp_config()

    if not all([host, user, password]):
        logger.debug("Email not sent (SMTP not configured): subject=%r to=%r", subject, to)
        return False

    msg = MIMEMultipart()
    msg["From"] = user  # type: ignore[assignment]
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if attachment_bytes and attachment_filename:
        part = MIMEBase(*attachment_content_type.split("/", 1))
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_filename}"',
        )
        msg.attach(part)

    try:
        with smtplib.SMTP(host, port) as server:  # type: ignore[arg-type]
            server.starttls()
            server.login(user, password)  # type: ignore[arg-type]
            server.sendmail(user, to, msg.as_string())  # type: ignore[arg-type]
        logger.info("Email sent: subject=%r to=%r", subject, to)
        return True
    except Exception as exc:
        logger.error("Email send failed: subject=%r to=%r error=%s", subject, to, exc)
        return False
