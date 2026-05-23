"""
extractor/groq_extractor.py
────────────────────────────
Fallback extraction using Groq (free tier, separate vendor from Cerebras).
Only called when Cerebras confidence < 0.95 or a critical field is null.

Free tier: 14,400 req/day on llama-3.3-70b-versatile
Get key  : https://console.groq.com — no credit card needed
Set in Render env: GROQ_API_KEY=your_key
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from groq import Groq

from extractor.cerebras_extractor import extract_text_from_pdf  # shared, no duplication
from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.groq")

PRIMARY_MODEL = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"  # faster backup if 70b is rate-limited


def _get_client() -> Groq:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise OSError("GROQ_API_KEY not set. Free key at https://console.groq.com")
    return Groq(api_key=key)


def _call_groq(
    text: str,
    model: str,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> dict | None:
    if not text or len(text.strip()) < 50:
        logger.error("[groq] Text too short (%d chars)", len(text))
        return None

    user_prompt = build_extraction_prompt(page_count, char_count)
    messages = [
        {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{user_prompt}\n\n" f"Invoice text extracted from PDF:\n\n" f"{text[:12000]}"
            ),
        },
    ]

    for attempt in range(1, max_retries + 2):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.0,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                if attempt <= max_retries:
                    time.sleep(2)
                continue

            result = sanitize(json.loads(raw))
            logger.info(
                "[groq/%s] OK — conf=%.2f  vendor=%s  invoice=%s  total=%s",
                model,
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result

        except json.JSONDecodeError as e:
            logger.warning("[groq/%s] JSON error attempt %d: %s", model, attempt, e)
            if attempt <= max_retries:
                time.sleep(2)

        except Exception as e:
            err = str(e)

            # Invalid key — fail immediately
            if "401" in err or "invalid_api_key" in err.lower():
                logger.error("[groq] Invalid API key — get free key at console.groq.com")
                return None

            # Rate limit — wait then retry
            if "429" in err or "rate_limit" in err.lower():
                wait = 30
                m = re.search(r"try again in ([\d.]+)s", err)
                if m:
                    wait = int(float(m.group(1))) + 2
                if attempt <= max_retries:
                    logger.warning("[groq/%s] Rate limited — waiting %ds", model, wait)
                    time.sleep(wait)
                else:
                    # If 70b rate-limited on all retries, try the 8b model once
                    if model == PRIMARY_MODEL:
                        logger.info("[groq] Dropping to %s after rate limit", FALLBACK_MODEL)
                        return _call_groq(
                            text, FALLBACK_MODEL, page_count, char_count, max_retries=1
                        )
                    return None

            else:
                logger.error("[groq/%s] Error attempt %d: %s", model, attempt, e)
                if attempt <= max_retries:
                    time.sleep(2**attempt)

    logger.error("[groq/%s] All attempts failed", model)
    return None


# ─────────────────────────────────────────────────────────────
#  Public API — called by pipeline.py
# ─────────────────────────────────────────────────────────────


def extract_fallback(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """Fallback: same pdfplumber text extraction → Groq llama-3.3-70b-versatile."""
    logger.info("Fallback extraction → groq/%s", PRIMARY_MODEL)
    text = extract_text_from_pdf(pdf_bytes)
    return _call_groq(text, PRIMARY_MODEL, page_count, len(text), max_retries=2)


def needs_fallback(result: dict | None, threshold: float = 0.95) -> bool:
    """Returns True if Cerebras result needs Groq to supplement it."""
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
