"""
extractor/gemini_extractor.py
──────────────────────────────
Extraction using the NEW google-genai SDK (not the deprecated google.generativeai).

Primary  → gemini-2.0-flash  (free, fast)
Fallback → gemini-1.5-pro    (free, more thorough — triggered if confidence < 0.80)

Free tier limits (Google AI Studio key):
  gemini-2.0-flash : 15 RPM, 1500 RPD
  gemini-1.5-pro   : 2 RPM, 50 RPD

Get your free key → https://aistudio.google.com/app/apikey
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from typing import Optional

from google import genai
from google.genai import types

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.gemini")

PRIMARY_MODEL  = "gemini-2.0-flash"
FALLBACK_MODEL = "gemini-1.5-pro"


# ─────────────────────────────────────────────────────────────
#  Client (created once per call — thread safe)
# ─────────────────────────────────────────────────────────────

def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY is not set. "
            "Get a free key at https://aistudio.google.com/app/apikey"
        )
    return genai.Client(api_key=api_key)


# ─────────────────────────────────────────────────────────────
#  Core extraction — shared by primary and fallback
# ─────────────────────────────────────────────────────────────

def _extract_with_model(
    pdf_bytes: bytes,
    model_name: str,
    page_count: int = 1,
    char_count: int = 0,
    max_retries: int = 2,
) -> Optional[dict]:
    """
    Send the PDF to Gemini and get structured invoice JSON back.
    Returns a sanitized dict or None on failure.
    """
    client = _get_client()
    user_prompt = build_extraction_prompt(page_count, char_count)

    config = types.GenerateContentConfig(
        system_instruction=INVOICE_SYSTEM_PROMPT,
        temperature=0.1,           # low = deterministic extraction
        max_output_tokens=4096,
        response_mime_type="application/json",   # force JSON output
    )

    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    # Send PDF bytes inline — no temp file needed
                    types.Part.from_bytes(
                        data=pdf_bytes,
                        mime_type="application/pdf",
                    ),
                    user_prompt,
                ],
                config=config,
            )

            raw_text = (response.text or "").strip()
            if not raw_text:
                logger.warning("[%s] Empty response on attempt %d", model_name, attempt + 1)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            # Strip markdown fences if model ignores the mime_type instruction
            raw_text = (
                raw_text
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )

            raw_data = json.loads(raw_text)
            result = sanitize(raw_data)
            logger.info(
                "[%s] Extraction OK — confidence=%.2f",
                model_name,
                result.get("ai_confidence", 0),
            )
            return result

        except json.JSONDecodeError as exc:
            logger.warning(
                "[%s] JSON parse error on attempt %d: %s",
                model_name, attempt + 1, exc,
            )
            if attempt < max_retries:
                time.sleep(2 ** attempt)

        except Exception as exc:
            logger.error(
                "[%s] Error on attempt %d: %s",
                model_name, attempt + 1, exc,
            )
            logger.debug(traceback.format_exc())
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    logger.error("[%s] All %d attempts failed", model_name, max_retries + 1)
    return None


# ─────────────────────────────────────────────────────────────
#  Public API  (imported by pipeline.py)
# ─────────────────────────────────────────────────────────────

def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """Primary extraction using Gemini Flash (free, fast)."""
    logger.info("Primary extraction starting → %s", PRIMARY_MODEL)
    return _extract_with_model(
        pdf_bytes=pdf_bytes,
        model_name=PRIMARY_MODEL,
        page_count=page_count,
        char_count=char_count,
        max_retries=2,
    )


def extract_fallback(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """Fallback extraction using Gemini Pro (free, more thorough)."""
    logger.info("Fallback extraction starting → %s", FALLBACK_MODEL)
    return _extract_with_model(
        pdf_bytes=pdf_bytes,
        model_name=FALLBACK_MODEL,
        page_count=page_count,
        char_count=char_count,
        max_retries=1,
    )


def needs_fallback(result: Optional[dict], threshold: float = 0.80) -> bool:
    """
    Returns True if the primary result should be supplemented by fallback.
    Triggers when confidence is below threshold OR any critical field is None.
    """
    if result is None:
        logger.info("Fallback needed: primary returned None")
        return True

    critical = ["vendor_name", "invoice_number", "invoice_date", "total_amount"]
    null_fields = [f for f in critical if result.get(f) is None]
    low_conf = result.get("ai_confidence", 0) < threshold

    if null_fields:
        logger.info("Fallback needed: null critical fields → %s", null_fields)
    if low_conf:
        logger.info(
            "Fallback needed: confidence %.2f < threshold %.2f",
            result.get("ai_confidence", 0), threshold,
        )

    return bool(null_fields) or low_conf