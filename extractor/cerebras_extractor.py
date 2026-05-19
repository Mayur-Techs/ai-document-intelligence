"""
extractor/cerebras_extractor.py
────────────────────────────────
Primary extraction using Cerebras Cloud (free tier).

Why Cerebras:
  - Completely free at cloud.cerebras.ai (no credit card)
  - Fastest LLM inference available: 1000-2000 tokens/sec
  - Same Llama 3.3 70B model quality as Groq
  - Separate vendor from Groq so quota issues on one don't kill both

Free tier: generous daily limits, no rate-limit surprises
Get key  : https://cloud.cerebras.ai → API Keys → Create

Set in Render env: CEREBRAS_API_KEY=your_key
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
from cerebras.cloud.sdk import Cerebras

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.cerebras")

MODEL = "llama-3.3-70b"


# ─────────────────────────────────────────────────────────────
#  PDF text extraction  (local, no API)
# ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Pull text from every page using pdfplumber. No API call needed."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                for table in page.extract_tables():
                    for row in table:
                        row_text = "  |  ".join(
                            str(c).strip() if c else "" for c in row
                        )
                        if row_text.strip(" |"):
                            text += "\n" + row_text
                pages.append(f"--- PAGE {i} ---\n{text}")
            full = "\n\n".join(pages).strip()
            logger.info("pdfplumber: %d chars from %d pages", len(full), len(pdf.pages))
            return full
    except Exception as e:
        logger.error("pdfplumber failed: %s", e)
        return ""


# ─────────────────────────────────────────────────────────────
#  Cerebras call
# ─────────────────────────────────────────────────────────────

def _get_client() -> Cerebras:
    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        raise EnvironmentError(
            "CEREBRAS_API_KEY not set. "
            "Free key at https://cloud.cerebras.ai — no credit card needed."
        )
    return Cerebras(api_key=key)


def _call_cerebras(
    text: str,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> Optional[dict]:
    if not text or len(text.strip()) < 50:
        logger.error("Text too short (%d chars) — PDF may be image-only", len(text))
        return None

    client      = _get_client()
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

    for attempt in range(1, max_retries + 2):
        try:
            response = _get_client().chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.0,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )

            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                logger.warning("[cerebras] Empty response attempt %d", attempt)
                if attempt <= max_retries:
                    time.sleep(2)
                continue

            raw_data = json.loads(raw)
            result   = sanitize(raw_data)
            logger.info(
                "[cerebras] OK — confidence=%.2f  vendor=%s  invoice=%s  total=%s",
                result.get("ai_confidence", 0),
                result.get("vendor_name"),
                result.get("invoice_number"),
                result.get("total_amount"),
            )
            return result

        except json.JSONDecodeError as e:
            logger.warning("[cerebras] JSON error attempt %d: %s", attempt, e)
            if attempt <= max_retries:
                time.sleep(2)

        except Exception as e:
            err = str(e)
            if "401" in err or "invalid" in err.lower():
                logger.error(
                    "[cerebras] Invalid API key. Get free key at https://cloud.cerebras.ai"
                )
                return None
            if "429" in err or "rate" in err.lower():
                wait = 30
                import re
                m = re.search(r'(\d+)s', err)
                if m:
                    wait = int(m.group(1)) + 2
                if attempt <= max_retries:
                    logger.warning("[cerebras] Rate limited — waiting %ds", wait)
                    time.sleep(wait)
                else:
                    return None
            else:
                logger.error("[cerebras] Error attempt %d: %s", attempt, e)
                if attempt <= max_retries:
                    time.sleep(2 ** attempt)

    logger.error("[cerebras] All attempts failed")
    return None


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────

def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> Optional[dict]:
    """Primary: pdfplumber text + Cerebras Llama 3.3 70B (free, ultra-fast)."""
    logger.info("Primary extraction → pdfplumber + cerebras/%s", MODEL)
    text = extract_text_from_pdf(pdf_bytes)
    return _call_cerebras(text, page_count, len(text), max_retries=2)