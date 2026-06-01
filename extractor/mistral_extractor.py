"""
extractor/mistral_extractor.py

Fallback AI extraction using Mistral (mistral-small-latest).
Free tier: 1 req/sec, 500,000 tokens/month, no credit card required.
Uses run_in_executor because Mistral SDK is synchronous.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

try:
    from mistralai import Mistral
except ImportError:
    try:
        from mistralai.client import Mistral
    except ImportError:
        from mistralai import Mistral

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize
from extractor.pdf_reader import extract_text_from_pdf

logger = logging.getLogger("docai.mistral")

_MAX_RETRIES = 2
_RETRY_DELAY = 2.0


def _call_mistral_sync(client: Mistral, messages: list) -> dict:
    """Synchronous Mistral call — always run via run_in_executor, never directly."""
    response = client.chat.complete(
        model="mistral-small-latest",
        messages=messages,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content.strip())


async def extract_mistral(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """
    Fallback extraction via Mistral mistral-small-latest.
    Returns sanitized extraction dict or None on failure.
    """
    key = os.getenv("MISTRAL_API_KEY")
    if not key:
        logger.error("MISTRAL_API_KEY not set — skipping Mistral extraction")
        return None

    text = extract_text_from_pdf(pdf_bytes)
    if not text or len(text.strip()) < 50:
        logger.error("PDF text too short for Mistral extraction")
        return None

    client = Mistral(api_key=key)
    user_prompt = build_extraction_prompt(page_count, len(text))
    messages = [
        {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
        {"role": "user", "content": f"{user_prompt}\n\n{text[:12000]}"},
    ]

    loop = asyncio.get_event_loop()

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = await loop.run_in_executor(None, lambda: _call_mistral_sync(client, messages))
            result = sanitize(raw)
            logger.info(
                "[mistral] OK — conf=%.2f vendor=%s invoice=%s total=%s",
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result
        except Exception as exc:
            logger.error("[mistral] Attempt %d/%d failed: %s", attempt, _MAX_RETRIES, exc)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY)

    return None


# Alias for pipeline.py compatibility
extract_fallback = extract_mistral
