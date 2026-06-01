"""
extractor/groq_extractor.py

Primary AI extraction using Groq (llama-3.3-70b-versatile).
Free tier: 14,400 requests/day.
Uses AsyncGroq — truly non-blocking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from groq import AsyncGroq

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize
from extractor.pdf_reader import extract_text_from_pdf

logger = logging.getLogger("docai.groq")

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds between retries


async def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """
    Primary extraction via Groq llama-3.3-70b-versatile.
    Returns sanitized extraction dict or None on failure.
    """
    key = os.getenv("GROQ_API_KEY")
    if not key:
        logger.error("GROQ_API_KEY not set — skipping Groq extraction")
        return None

    text = extract_text_from_pdf(pdf_bytes)
    if not text or len(text.strip()) < 50:
        logger.error("PDF text too short for Groq extraction (got %d chars)", len(text))
        return None

    client = AsyncGroq(api_key=key)
    user_prompt = build_extraction_prompt(page_count, len(text))

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
                    {"role": "user", "content": f"{user_prompt}\n\n{text[:12000]}"},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            result = sanitize(json.loads(raw))
            logger.info(
                "[groq] OK — conf=%.2f vendor=%s invoice=%s total=%s",
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result
        except Exception as exc:
            logger.error("[groq] Attempt %d/%d failed: %s", attempt, _MAX_RETRIES, exc)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY)

    return None


def needs_fallback(result: dict | None, threshold: float = 0.95) -> bool:
    """Returns True if Groq result needs Mistral fallback to supplement it."""
    if result is None:
        logger.info("Fallback needed: primary returned None")
        return True
    critical = ["vendor_name", "invoice_number", "invoice_date", "total_amount"]
    null_fields = [f for f in critical if result.get(f) is None]
    low_conf = result.get("ai_confidence", 0) < threshold
    if null_fields:
        logger.info("Fallback needed: null fields → %s", null_fields)
    if low_conf:
        logger.info("Fallback needed: conf %.2f < %.2f", result.get("ai_confidence", 0), threshold)
    return bool(null_fields) or low_conf


# Keep backward-compatible alias used by pipeline.py
extract_fallback = extract_primary
