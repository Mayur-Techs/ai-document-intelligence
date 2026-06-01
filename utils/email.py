"""
utils/email.py

Single SMTP helper for the entire application.
All email sending goes through send_email() — never inline smtplib.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("docai.email")


def is_email_configured() -> bool:
    """Return True if SMTP env vars are present."""
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    return bool(user and password)


def send_email(
    to: str,
    subject: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
) -> bool:
    """
    Send an email via SMTP. Returns True on success, False on failure.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain text body.
        attachment_bytes: Optional file bytes to attach.
        attachment_filename: Filename for the attachment.
    """
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")

    if not user or not password:
        logger.error("SMTP credentials not configured (SMTP_USER / SMTP_PASSWORD missing)")
        return False

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if attachment_bytes and attachment_filename:
        part = MIMEApplication(attachment_bytes, Name=attachment_filename)
        part["Content-Disposition"] = f'attachment; filename="{attachment_filename}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(user, to, msg.as_string())
        logger.info("Email sent to %s — subject: %s", to, subject)
        return True
    except Exception as exc:
        logger.error("Email send failed to %s: %s", to, exc)
        return False
