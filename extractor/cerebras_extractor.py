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

Fix history:
  2026-05-29 — Switched from json_object → json_schema + strict=True.
               Constrained decoding makes truncated/malformed JSON
               architecturally impossible. Also bumped max_tokens to
               4096 and added llama-3.3-70b as primary model.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time

import pdfplumber
from cerebras.cloud.sdk import Cerebras

from extractor.extraction_prompts import INVOICE_SYSTEM_PROMPT, build_extraction_prompt
from extractor.field_sanitizer import sanitize

logger = logging.getLogger("docai.cerebras")

# Try the newer model first; fall back if 404
PRIMARY_MODEL = "llama3.3-70b"
FALLBACK_MODEL = "llama3.1-8b"  # smallest always-available model as last resort


# ─────────────────────────────────────────────────────────────
#  Strict JSON schema — constrained decoding stops truncation
#  All properties required + additionalProperties=false per Cerebras docs
# ─────────────────────────────────────────────────────────────

LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": ["string", "null"]},
        "hsn":         {"type": ["string", "null"]},
        "quantity":    {"type": ["number", "null"]},
        "unit":        {"type": ["string", "null"]},
        "unit_price":  {"type": ["number", "null"]},
        "amount":      {"type": ["number", "null"]},
    },
    "required": ["description", "hsn", "quantity", "unit", "unit_price", "amount"],
    "additionalProperties": False,
}

INVOICE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name":          {"type": ["string", "null"]},
        "vendor_gstin":         {"type": ["string", "null"]},
        "buyer_name":           {"type": ["string", "null"]},
        "buyer_gstin":          {"type": ["string", "null"]},
        "invoice_number":       {"type": ["string", "null"]},
        "invoice_date":         {"type": ["string", "null"]},
        "due_date":             {"type": ["string", "null"]},
        "currency":             {"type": ["string", "null"]},
        "subtotal":             {"type": ["number", "null"]},
        "tax_amount":           {"type": ["number", "null"]},
        "total_amount":         {"type": ["number", "null"]},
        "bank_ifsc":            {"type": ["string", "null"]},
        "bank_account_number":  {"type": ["string", "null"]},
        "bank_name":            {"type": ["string", "null"]},
        "line_items":           {"type": "array", "items": LINE_ITEM_SCHEMA},
        "confidence":           {"type": "number"},
        "confidence_reason":    {"type": ["string", "null"]},
    },
    "required": [
        "vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin",
        "invoice_number", "invoice_date", "due_date", "currency",
        "subtotal", "tax_amount", "total_amount",
        "bank_ifsc", "bank_account_number", "bank_name",
        "line_items", "confidence", "confidence_reason",
    ],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────
#  PDF text extraction  (local, no API)
# ─────────────────────────────────────────────────────────────


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Pull text from every page using pdfplumber. Fallback to PyMuPDF (fitz)."""
    full_text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                for table in page.extract_tables():
                    for row in table:
                        row_text = "  |  ".join(str(c).strip() if c else "" for c in row)
                        if row_text.strip(" |"):
                            text += "\n" + row_text
                pages.append(f"--- PAGE {i} ---\n{text}")
            full_text = "\n\n".join(pages).strip()
            logger.info("pdfplumber: %d chars from %d pages", len(full_text), len(pdf.pages))
    except Exception as e:
        logger.error("pdfplumber failed: %s", e)

    if len(full_text.strip()) > 50:
        return full_text

    # Try PyMuPDF (fitz) fallback
    try:
        import fitz

        logger.info(
            "pdfplumber gave sparse text (%d chars) — trying PyMuPDF fallback...",
            len(full_text),
        )
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = []
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text") or ""
                pages.append(f"--- PAGE {i} ---\n{text}")
            full_text = "\n\n".join(pages).strip()
            logger.info("PyMuPDF fallback: %d chars from %d pages", len(full_text), len(doc))
    except Exception as e:
        logger.error("PyMuPDF fallback failed: %s", e)

    return full_text


# ─────────────────────────────────────────────────────────────
#  JSON repair — last-resort bracket closer for edge cases
# ─────────────────────────────────────────────────────────────


def _try_repair_json(raw: str) -> dict | None:
    """
    Attempt to repair truncated JSON by counting open brackets/braces
    and closing them. Only used when json_schema strict mode somehow fails.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Count unmatched brackets
    stack = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    # Close any open string
    if in_string:
        raw += '"'

    # Append missing closers
    raw += "".join(reversed(stack))

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────
#  Cerebras call — strict json_schema mode
# ─────────────────────────────────────────────────────────────


def _get_client() -> Cerebras:
    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        raise OSError(
            "CEREBRAS_API_KEY not set. "
            "Free key at https://cloud.cerebras.ai — no credit card needed."
        )
    return Cerebras(api_key=key)


def _call_cerebras(
    text: str,
    page_count: int,
    char_count: int,
    max_retries: int = 2,
) -> dict | None:
    if not text or len(text.strip()) < 50:
        logger.error("Text too short (%d chars) — PDF may be image-only", len(text))
        return None

    client = _get_client()
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

    # Use json_schema with strict=True — constrained decoding guarantees
    # the model can never produce truncated or malformed JSON.
    # This is the Cerebras-recommended approach per their 2025 docs.
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "invoice_extraction",
            "strict": True,
            "schema": INVOICE_JSON_SCHEMA,
        },
    }

    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]

    for model in models_to_try:
        for attempt in range(1, max_retries + 2):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=4096,   # raised from 2048 — line items need space
                    response_format=response_format,
                )

                raw = (response.choices[0].message.content or "").strip()
                if not raw:
                    logger.warning("[cerebras/%s] Empty response attempt %d", model, attempt)
                    if attempt <= max_retries:
                        time.sleep(2)
                    continue

                # With strict json_schema, json.loads should always succeed.
                # _try_repair_json is a belt-and-suspenders safety net.
                try:
                    raw_data = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "[cerebras/%s] Unexpected JSON error (strict mode): %s — trying repair",
                        model, e,
                    )
                    raw_data = _try_repair_json(raw)
                    if raw_data is None:
                        if attempt <= max_retries:
                            time.sleep(2)
                        continue

                result = sanitize(raw_data)
                logger.info(
                    "[cerebras/%s] OK — conf=%.2f  vendor=%s  invoice=%s  total=%s",
                    model,
                    result.get("ai_confidence", 0),
                    result.get("vendor_name"),
                    result.get("invoice_number"),
                    result.get("total_amount"),
                )
                return result

            except Exception as e:
                err = str(e)
                if "404" in err or "model_not_found" in err.lower() or "not found" in err.lower():
                    logger.warning("[cerebras] Model %s not found — trying next model", model)
                    break  # try next model
                if "401" in err or "invalid" in err.lower():
                    logger.error(
                        "[cerebras] Invalid API key. Get free key at https://cloud.cerebras.ai"
                    )
                    return None
                if "429" in err or "rate" in err.lower():
                    wait = 30
                    m = re.search(r"(\d+)s", err)
                    if m:
                        wait = int(m.group(1)) + 2
                    if attempt <= max_retries:
                        logger.warning("[cerebras/%s] Rate limited — waiting %ds", model, wait)
                        time.sleep(wait)
                    else:
                        break  # try next model
                else:
                    logger.error("[cerebras/%s] Error attempt %d: %s", model, attempt, e)
                    if attempt <= max_retries:
                        time.sleep(2 ** attempt)

    logger.error("[cerebras] All models and attempts failed")
    return None


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────


def extract_primary(
    pdf_bytes: bytes,
    page_count: int = 1,
    char_count: int = 0,
) -> dict | None:
    """Primary: pdfplumber text + Cerebras Llama 3.3 70B (free, ultra-fast)."""
    logger.info("Primary extraction → pdfplumber + cerebras/%s", PRIMARY_MODEL)
    text = extract_text_from_pdf(pdf_bytes)
    return _call_cerebras(text, page_count, len(text), max_retries=2)
