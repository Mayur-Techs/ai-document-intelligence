"""
extractor/groq_extractor.py
────────────────────────────
Extraction pipeline using:
  Step 1 → pdfplumber  (local, free, already installed) — extracts raw text from PDF
  Step 2 → Groq API    (free tier: 14,400 req/day, no credit card needed)
             Primary model  : llama-3.3-70b-versatile  (smart, handles JSON well)
             Fallback model : llama-3.1-8b-instant     (faster, still free)

Why Groq instead of Gemini:
  - Gemini free tier has limit=0 on Cloud Console keys and 404 model issues
  - Groq free tier is genuinely unlimited for our use case
  - pdfplumber already extracts text — no vision API needed

Get free Groq key → https://console.groq.com  (sign up, no credit card)
Set in Render env  → GROQ_API_KEY=your_key
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import traceback
from typing import Optional

import pdfplumber
from groq import Groq

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.groq")

PRIMARY_MODEL  = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"


# ─────────────────────────────────────────────────────────────
#  Step 1 — Extract text from PDF bytes using pdfplumber
# ─────────────────────────────────────────────────────────────

def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Use pdfplumber to pull raw text from every page of the PDF.
    Returns concatenated text string. Totally local, no API call.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                # Also extract tables as plain text rows
                for table in page.extract_tables():
                    for row in table:
                        clean_row = "  |  ".join(
                            str(cell).strip() if cell else ""
                            for cell in row
                        )
                        if clean_row.strip(" |"):
                            text += "\n" + clean_row
                pages.append(f"--- PAGE {i} ---\n{text}")

            full_text = "\n\n".join(pages).strip()
            logger.info(
                "pdfplumber extracted %d chars from %d pages",
                len(full_text), len(pdf.pages),
            )
            return full_text

    except Exception as e:
        logger.error("pdfplumber extraction failed: %s", e)
        return ""


# ─────────────────────────────────────────────────────────────
#  Step 2 — Send text to Groq LLM
# ─────────────────────────────────────────────────────────────

def _get_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY is not set. "
            "Get a free key at https://console.groq.com — no credit card needed."
        )
    return Groq(api_key=api_key)


def _call_groq(
    text: str,
    model_name: str,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> Optional[dict]:
    """Send extracted invoice text to Groq and get structured JSON back."""

    if not text or len(text.strip()) < 50:
        logger.error("Text too short to extract from (%d chars) — PDF may be scanned/image-only", len(text))
        return None

    client      = _get_client()
    user_prompt = build_extraction_prompt(page_count, char_count)

    messages = [
        {"role": "system", "content": INVOICE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"{user_prompt}\n\n"
                f"Here is the full invoice text extracted from the PDF:\n\n"
                f"{text[:12000]}"   # Groq context limit safety — 12k chars is enough for any invoice
            ),
        },
    ]

    for attempt in range(1, max_retries + 2):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=2048,
                response_format={"type": "json_object"},  # forces JSON output
            )

            raw_text = (response.choices[0].message.content or "").strip()
            if not raw_text:
                logger.warning("[%s] Empty response on attempt %d", model_name, attempt)
                if attempt <= max_retries:
                    time.sleep(2)
                continue

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

            # Rate limit — wait and retry
            if "429" in err_str or "rate_limit" in err_str.lower():
                wait = 30
                import re
                m = re.search(r'try again in ([\d.]+)s', err_str)
                if m:
                    wait = int(float(m.group(1))) + 2
                if attempt <= max_retries:
                    logger.warning("[%s] Rate limited — waiting %ds", model_name, wait)
                    time.sleep(wait)
                else:
                    logger.error("[%s] Rate limited on all attempts", model_name)
                    return None

            # Auth error — no point retrying
            elif "401" in err_str or "invalid_api_key" in err_str.lower():
                logger.error(
                    "[%s] Invalid API key. Get a free key at https://console.groq.com",
                    model_name,
                )
                return None

            else:
                logger.error("[%s] Error attempt %d: %s", model_name, attempt, exc)
                if attempt <= max_retries:
                    time.sleep(2 ** attempt)

    logger.error("[%s] All attempts failed", model_name)
    return None


# ─────────────────────────────────────────────────────────────
#  Public API — called by pipeline.py
# ─────────────────────────────────────────────────────────────

def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """
    Primary extraction:
      1. pdfplumber pulls text from PDF locally
      2. llama-3.3-70b-versatile on Groq reads and structures it
    """
    logger.info("Primary extraction → pdfplumber + %s", PRIMARY_MODEL)
    text = _extract_text_from_pdf(pdf_bytes)
    return _call_groq(
        text=text,
        model_name=PRIMARY_MODEL,
        page_count=page_count,
        char_count=len(text),
        max_retries=2,
    )


def extract_fallback(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """
    Fallback extraction — different (faster) Groq model.
    Called only when primary confidence < 0.80 or critical fields are null.
    """
    logger.info("Fallback extraction → pdfplumber + %s", FALLBACK_MODEL)
    text = _extract_text_from_pdf(pdf_bytes)
    return _call_groq(
        text=text,
        model_name=FALLBACK_MODEL,
        page_count=page_count,
        char_count=len(text),
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
            "Fallback needed: confidence %.2f < %.2f",
            result.get("ai_confidence", 0), threshold,
        )
    return bool(null_fields) or low_conf