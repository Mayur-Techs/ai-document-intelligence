"""
gemini_extractor.py
────────────────────
Primary and fallback extraction using Google Gemini (free tier).

Primary  → gemini-2.0-flash  (fast, free, handles most invoices)
Fallback → gemini-1.5-pro    (slower but more thorough, for complex/low-confidence docs)

Both models accept PDF bytes natively via the File API or inline base64.
Free tier limits (Google AI Studio key):
  • gemini-2.0-flash : 15 RPM, 1 500 RPD, 1M tokens/day
  • gemini-1.5-pro   : 2 RPM, 50 RPD

Set GEMINI_API_KEY in your environment or .env file.
Get a free key at: https://aistudio.google.com/app/apikey
"""

import os
import json
import time
import base64
import logging
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from field_sanitizer import sanitize

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Model names
# ──────────────────────────────────────────────
PRIMARY_MODEL  = "gemini-2.0-flash"
FALLBACK_MODEL = "gemini-1.5-pro"

# ──────────────────────────────────────────────
#  Safety settings — turn off for business docs
# ──────────────────────────────────────────────
SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT:        HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH:       HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# ──────────────────────────────────────────────
#  Initialise SDK
# ──────────────────────────────────────────────

def _init_gemini() -> None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY not set. Get a free key at "
            "https://aistudio.google.com/app/apikey and add it to your .env"
        )
    genai.configure(api_key=api_key)


# ──────────────────────────────────────────────
#  Core extraction helper
# ──────────────────────────────────────────────

def _extract_with_model(
    pdf_bytes: bytes,
    model_name: str,
    page_count: int = 1,
    char_count: int = 0,
    retry: int = 2,
) -> Optional[dict]:
    """
    Upload PDF to Gemini and extract structured invoice data.
    Returns sanitized dict or None on failure.
    """
    _init_gemini()

    generation_config = genai.GenerationConfig(
        temperature=0.1,        # low temp = more deterministic extraction
        top_p=0.9,
        max_output_tokens=4096,
        response_mime_type="application/json",   # enforce JSON output
    )

    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=INVOICE_SYSTEM_PROMPT,
        generation_config=generation_config,
        safety_settings=SAFETY_SETTINGS,
    )

    # Upload PDF via File API (handles large files better than inline base64)
    uploaded_file = None
    for attempt in range(retry + 1):
        try:
            # Upload the PDF bytes
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name

            uploaded_file = genai.upload_file(
                path=tmp_path,
                mime_type="application/pdf",
                display_name=f"invoice_{int(time.time())}.pdf",
            )

            # Wait for file to be processed
            while uploaded_file.state.name == "PROCESSING":
                time.sleep(1)
                uploaded_file = genai.get_file(uploaded_file.name)

            if uploaded_file.state.name == "FAILED":
                raise ValueError("Gemini file processing failed")

            # Build the prompt
            user_prompt = build_extraction_prompt(page_count, char_count)

            # Call the model
            response = model.generate_content(
                [uploaded_file, user_prompt]
            )

            # Clean up uploaded file
            try:
                genai.delete_file(uploaded_file.name)
                os.unlink(tmp_path)
            except Exception:
                pass

            if not response.text:
                logger.warning(f"[{model_name}] Empty response on attempt {attempt+1}")
                continue

            # Parse JSON
            raw_text = response.text.strip()
            # Strip markdown fences if model ignores the mime type instruction
            raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

            raw_data = json.loads(raw_text)
            return sanitize(raw_data)

        except json.JSONDecodeError as e:
            logger.warning(f"[{model_name}] JSON parse error on attempt {attempt+1}: {e}")
            if attempt < retry:
                time.sleep(2 ** attempt)
            continue

        except Exception as e:
            logger.error(f"[{model_name}] Extraction error on attempt {attempt+1}: {e}")
            if uploaded_file:
                try:
                    genai.delete_file(uploaded_file.name)
                except Exception:
                    pass
            if attempt < retry:
                time.sleep(2 ** attempt)
            continue

    return None


# ──────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────

def extract_primary(pdf_bytes: bytes, page_count: int = 1, char_count: int = 0) -> Optional[dict]:
    """
    Primary extraction using Gemini Flash (free, fast).
    Returns sanitized dict or None.
    """
    logger.info(f"[Primary] Running gemini-2.0-flash extraction...")
    result = _extract_with_model(
        pdf_bytes=pdf_bytes,
        model_name=PRIMARY_MODEL,
        page_count=page_count,
        char_count=char_count,
        retry=2,
    )
    if result:
        logger.info(f"[Primary] Confidence: {result.get('ai_confidence', 0):.2f}")
    return result


def extract_fallback(pdf_bytes: bytes, page_count: int = 1, char_count: int = 0) -> Optional[dict]:
    """
    Fallback extraction using Gemini Pro (free tier, more thorough).
    Called when primary confidence < threshold or critical fields are null.
    """
    logger.info(f"[Fallback] Running gemini-1.5-pro extraction (deeper analysis)...")
    result = _extract_with_model(
        pdf_bytes=pdf_bytes,
        model_name=FALLBACK_MODEL,
        page_count=page_count,
        char_count=char_count,
        retry=1,        # Pro is slower, limit retries
    )
    if result:
        logger.info(f"[Fallback] Confidence: {result.get('ai_confidence', 0):.2f}")
    return result


def needs_fallback(result: dict, threshold: float = 0.80) -> bool:
    """
    Decide if a primary extraction result needs the fallback model.
    Triggers fallback if:
      • confidence < threshold, OR
      • any critical field is None
    """
    if result is None:
        return True

    critical_fields = ['vendor_name', 'invoice_number', 'invoice_date', 'total_amount']
    has_null_critical = any(result.get(f) is None for f in critical_fields)
    low_confidence = result.get('ai_confidence', 0) < threshold

    if low_confidence:
        logger.info(f"Fallback triggered: confidence {result.get('ai_confidence'):.2f} < {threshold}")
    if has_null_critical:
        nulls = [f for f in critical_fields if result.get(f) is None]
        logger.info(f"Fallback triggered: null critical fields → {nulls}")

    return low_confidence or has_null_critical
