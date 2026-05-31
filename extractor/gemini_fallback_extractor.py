"""
extractor/gemini_fallback_extractor.py
────────────────────────────────────────
Third-tier (final) fallback using Google Gemini 2.0 Flash (free tier).
Called when both Groq and Mistral return None or low confidence.

Gemini handles PDF bytes natively — no pdfplumber needed here.

Free tier: gemini-2.0-flash via Google AI Studio
Get key  : https://aistudio.google.com/app/apikey — no credit card needed
Set in Render env: GEMINI_API_KEY=your_key
"""

from __future__ import annotations

import json
import logging
import os
import time

from google import genai
from google.genai import types

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.gemini")

MODEL = "gemini-2.0-flash"


def _get_client() -> genai.Client:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise OSError("GEMINI_API_KEY not set. Free key at https://aistudio.google.com/app/apikey")
    return genai.Client(api_key=key)


def _call_gemini(
    pdf_bytes: bytes,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> dict | None:
    if not pdf_bytes:
        logger.error("[gemini] No PDF bytes provided")
        return None

    user_prompt = build_extraction_prompt(page_count, char_count)
    full_prompt = (
        f"{user_prompt}\n\n"
        f"The PDF document is attached. Extract invoice data from it."
    )

    # Build content parts: text prompt + inline PDF bytes
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

    config = types.GenerateContentConfig(
        system_instruction=INVOICE_SYSTEM_PROMPT,
        temperature=0.0,
        max_output_tokens=2048,
        response_mime_type="application/json",
    )

    for attempt in range(1, max_retries + 2):
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=MODEL,
                contents=[full_prompt, pdf_part],
                config=config,
            )
            raw = (response.text or "").strip()

            # Strip markdown code fences if present
            if raw.startswith("```"):
                lines = raw.split("\n")
                inner_lines = []
                in_fence = False
                for line in lines:
                    if line.startswith("```"):
                        in_fence = not in_fence
                        continue
                    inner_lines.append(line)
                raw = "\n".join(inner_lines).strip()

            if not raw:
                logger.warning("[gemini] Empty response attempt %d", attempt)
                if attempt <= max_retries:
                    time.sleep(2)
                continue

            result = sanitize(json.loads(raw))
            logger.info(
                "[gemini/%s] OK — conf=%.2f  vendor=%s  invoice=%s  total=%s",
                MODEL,
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result

        except json.JSONDecodeError as e:
            logger.warning("[gemini] JSON error attempt %d: %s", attempt, e)
            if attempt <= max_retries:
                time.sleep(2)

        except Exception as e:
            err = str(e)

            # Invalid key
            if "401" in err or "api_key" in err.lower() or "api key" in err.lower():
                logger.error("[gemini] Invalid API key — get free key at aistudio.google.com")
                return None

            # Quota / rate limit
            if "429" in err or "quota" in err.lower() or "resource_exhausted" in err.lower():
                logger.error("[gemini] Quota/rate limit hit. Cannot proceed with Gemini.")
                return None

            # Model not found
            if "404" in err or "not found" in err.lower():
                logger.error("[gemini] Model %s not found.", MODEL)
                return None

            logger.error("[gemini] Error attempt %d: %s", attempt, e)
            if attempt <= max_retries:
                time.sleep(2**attempt)

    logger.error("[gemini] All attempts failed")
    return None


# ─────────────────────────────────────────────────────────────
#  Public API — called by pipeline.py
# ─────────────────────────────────────────────────────────────


def extract_gemini(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """Final fallback: PDF bytes → Gemini 2.0 Flash (native PDF understanding)."""
    logger.info("Gemini fallback extraction → %s", MODEL)
    return _call_gemini(pdf_bytes, page_count, char_count, max_retries=2)
