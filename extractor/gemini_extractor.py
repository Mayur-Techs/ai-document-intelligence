"""
extractor/gemini_extractor.py
──────────────────────────────
Uses google-genai SDK (NOT the deprecated google.generativeai).

Primary  → gemini-1.5-flash   (free tier: 1500 req/day, 15 req/min)
Fallback → gemini-2.0-flash   (free tier: 200 req/day — used only when primary fails)

IMPORTANT: API key MUST come from https://aistudio.google.com/app/apikey
           Keys from Google Cloud Console have limit=0 on free tier and will always fail.
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

# ─────────────────────────────────────────────────────────────
#  Model names — verified working with google-genai SDK
#  gemini-1.5-flash   → primary   (most generous free tier)
#  gemini-2.0-flash   → fallback  (higher quality, lower daily limit)
# ─────────────────────────────────────────────────────────────
PRIMARY_MODEL  = "gemini-1.5-flash"
FALLBACK_MODEL = "gemini-2.0-flash"


def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY is not set. "
            "Get a FREE key at https://aistudio.google.com/app/apikey  "
            "(use AI Studio — NOT Google Cloud Console)"
        )
    return genai.Client(api_key=api_key)


# ─────────────────────────────────────────────────────────────
#  Core extraction
# ─────────────────────────────────────────────────────────────

def _extract_with_model(
    pdf_bytes: bytes,
    model_name: str,
    page_count: int = 1,
    char_count: int = 0,
    max_retries: int = 2,
) -> Optional[dict]:

    client      = _get_client()
    user_prompt = build_extraction_prompt(page_count, char_count)

    config = types.GenerateContentConfig(
        system_instruction=INVOICE_SYSTEM_PROMPT,
        temperature=0.1,
        max_output_tokens=4096,
        response_mime_type="application/json",
    )

    for attempt in range(1, max_retries + 2):   # attempts = max_retries + 1
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
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
                logger.warning("[%s] Empty response on attempt %d", model_name, attempt)
                if attempt <= max_retries:
                    time.sleep(2)
                continue

            # Strip markdown fences if model ignores mime_type
            raw_text = (
                raw_text
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )

            raw_data = json.loads(raw_text)
            result   = sanitize(raw_data)
            logger.info(
                "[%s] OK — confidence=%.2f  vendor=%s  invoice=%s  total=%s",
                model_name,
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result

        except json.JSONDecodeError as exc:
            logger.warning("[%s] JSON parse error attempt %d: %s", model_name, attempt, exc)
            if attempt <= max_retries:
                time.sleep(2)

        except Exception as exc:
            err_str = str(exc)

            # ── 429 quota handling ─────────────────────────────────
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if "limit: 0" in err_str:
                    # This means the API key is from Google Cloud Console, not AI Studio.
                    # Retrying won't help — fail immediately with a clear message.
                    logger.error(
                        "[%s] QUOTA LIMIT=0: Your API key was created in Google Cloud Console. "
                        "It has zero free-tier quota. "
                        "Get a FREE key from https://aistudio.google.com/app/apikey instead.",
                        model_name,
                    )
                    return None

                # Normal rate limit — extract suggested retry delay and wait
                retry_delay = 60   # default wait
                import re
                m = re.search(r'retryDelay["\s:]+(\d+)', err_str)
                if m:
                    retry_delay = int(m.group(1)) + 2

                if attempt <= max_retries:
                    logger.warning(
                        "[%s] Rate limited (429) on attempt %d — waiting %ds before retry",
                        model_name, attempt, retry_delay,
                    )
                    time.sleep(retry_delay)
                else:
                    logger.error("[%s] Rate limited on all attempts — giving up", model_name)
                    return None

            # ── 404 model not found ────────────────────────────────
            elif "404" in err_str or "NOT_FOUND" in err_str:
                logger.error(
                    "[%s] Model not found (404). "
                    "Valid models: gemini-1.5-flash, gemini-2.0-flash, gemini-1.5-pro-latest",
                    model_name,
                )
                return None   # no point retrying a wrong model name

            else:
                logger.error("[%s] Error on attempt %d: %s", model_name, attempt, exc)
                logger.debug(traceback.format_exc())
                if attempt <= max_retries:
                    time.sleep(2 ** attempt)

    logger.error("[%s] All attempts failed", model_name)
    return None


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────

def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """Primary: gemini-1.5-flash — free tier, fast, 1500 req/day."""
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
    """Fallback: gemini-2.0-flash — better quality, used only when primary confidence < 0.80."""
    logger.info("Fallback extraction starting → %s", FALLBACK_MODEL)
    return _extract_with_model(
        pdf_bytes=pdf_bytes,
        model_name=FALLBACK_MODEL,
        page_count=page_count,
        char_count=char_count,
        max_retries=1,
    )


def needs_fallback(result: Optional[dict], threshold: float = 0.80) -> bool:
    """Returns True if primary result needs the fallback model."""
    if result is None:
        logger.info("Fallback needed: primary returned None")
        return True

    critical    = ["vendor_name", "invoice_number", "invoice_date", "total_amount"]
    null_fields = [f for f in critical if result.get(f) is None]
    low_conf    = result.get("ai_confidence", 0) < threshold

    if null_fields:
        logger.info("Fallback needed: null critical fields → %s", null_fields)
    if low_conf:
        logger.info(
            "Fallback needed: confidence %.2f < threshold %.2f",
            result.get("ai_confidence", 0), threshold,
        )

    return bool(null_fields) or low_conf