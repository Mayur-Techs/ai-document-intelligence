"""
extractor/mistral_extractor.py
───────────────────────────────
Second-tier extraction using Mistral AI (free tier).
Called when Groq returns None or confidence < threshold.

Free tier: mistral-small-latest via Mistral API
Get key  : https://console.mistral.ai — no credit card needed
Set in Render env: MISTRAL_API_KEY=your_key
"""

from __future__ import annotations

import json
import logging
import os
import time

from mistralai.client import Mistral

from extractor.cerebras_extractor import extract_text_from_pdf  # shared pdfplumber helper
from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.mistral")

MODEL = "mistral-small-latest"


def _get_client() -> Mistral:
    key = os.getenv("MISTRAL_API_KEY")
    if not key:
        raise OSError("MISTRAL_API_KEY not set. Free key at https://console.mistral.ai")
    return Mistral(api_key=key)


def _call_mistral(
    text: str,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> dict | None:
    if not text or len(text.strip()) < 50:
        logger.error("[mistral] Text too short (%d chars)", len(text))
        return None

    user_prompt = build_extraction_prompt(page_count, char_count)
    messages = [
        {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{user_prompt}\n\n"
                f"Invoice text extracted from PDF:\n\n"
                f"{text[:12000]}"
            ),
        },
    ]

    client = _get_client()

    for attempt in range(1, max_retries + 2):
        try:
            # Mistral SDK v2.x: client.chat.complete(...)
            response = client.chat.complete(
                model=MODEL,
                messages=messages,
                temperature=0.0,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                logger.warning("[mistral] Empty response attempt %d", attempt)
                if attempt <= max_retries:
                    time.sleep(2)
                continue

            result = sanitize(json.loads(raw))
            logger.info(
                "[mistral/%s] OK — conf=%.2f  vendor=%s  invoice=%s  total=%s",
                MODEL,
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result

        except json.JSONDecodeError as e:
            logger.warning("[mistral] JSON error attempt %d: %s", attempt, e)
            if attempt <= max_retries:
                time.sleep(2)

        except Exception as e:
            err = str(e)

            # Invalid key — fail immediately
            if "401" in err or "unauthorized" in err.lower() or "invalid_api_key" in err.lower():
                logger.error("[mistral] Invalid API key — get free key at console.mistral.ai")
                return None

            # Rate limit — bypass immediately
            if "429" in err or "rate" in err.lower() or "too many" in err.lower():
                logger.error(
                    "[mistral] Rate limit hit. Bypassing to avoid pipeline freeze."
                )
                return None

            # Model not available
            if "404" in err or "not found" in err.lower():
                logger.error("[mistral] Model %s not found or not available.", MODEL)
                return None

            logger.error("[mistral] Error attempt %d: %s", attempt, e)
            if attempt <= max_retries:
                time.sleep(2**attempt)

    logger.error("[mistral] All attempts failed")
    return None


# ─────────────────────────────────────────────────────────────
#  Public API — called by pipeline.py
# ─────────────────────────────────────────────────────────────


def extract_mistral(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """Second tier: pdfplumber text extraction → Mistral mistral-small-latest."""
    logger.info("Mistral extraction → %s", MODEL)
    text = extract_text_from_pdf(pdf_bytes)
    return _call_mistral(text, page_count, len(text), max_retries=2)
