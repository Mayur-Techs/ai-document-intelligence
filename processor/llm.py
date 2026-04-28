"""
processor/llm.py — Invoice field extraction via Claude Sonnet API.

WHY Claude Sonnet (not Haiku) for extraction?
Document understanding requires contextual reasoning — reading a line item,
understanding it's a freight charge, mapping it to the right field.
Haiku is fast and cheap but misses nuance in dense financial documents.
Sonnet is the right cost/quality tradeoff for extraction tasks.
Haiku is fine for binary classification (qualifier.py). Sonnet for extraction.

WHY enforce JSON output in the prompt?
Claude returns freeform text by default. For a pipeline that writes to a DB,
freeform text is useless. The prompt technique:
  "Return ONLY a JSON object. No preamble. No explanation. No markdown fences."
When the model knows the ONLY output format, it commits to it.
Parsing failure rate drops from ~15% to <1%.

WHY ask Claude to score its own confidence?
Invoices are messy — handwritten amounts, mixed languages, scanned PDFs.
Having Claude self-assess prevents silently storing wrong data.
confidence_score < 60 → flag for human review instead of auto-accepting.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("docai.processor.llm")

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Sonnet for extraction — needs contextual reasoning, not just classification
CLAUDE_MODEL = os.getenv("EXTRACTION_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 2048    # extraction responses are longer than classification


# --------------------------------------------------------------------------- #
# Pydantic model for Claude's output — validation happens here, not in the DB  #
# --------------------------------------------------------------------------- #

class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class InvoiceFields(BaseModel):
    """
    Validated output from Claude's extraction.

    WHY validate Claude's output with Pydantic?
    Claude can hallucinate field names or return wrong types.
    "total_amount": "₹45,000" instead of 45000.0
    Pydantic catches this immediately so we don't write garbage to the DB.
    """
    invoice_number: str | None = Field(None, description="Invoice/bill number")
    invoice_date: str | None = Field(None, description="Invoice date, any format")
    due_date: str | None = Field(None, description="Payment due date")
    currency: str = Field(default="INR")

    vendor_name: str | None = None
    vendor_address: str | None = None
    vendor_gstin: str | None = Field(None, description="GST Identification Number of vendor")

    buyer_name: str | None = None
    buyer_address: str | None = None
    buyer_gstin: str | None = None

    subtotal: float | None = None
    tax_amount: float | None = None
    discount_amount: float | None = None
    total_amount: float | None = None

    line_items: list[LineItem] = Field(default_factory=list)
    confidence_score: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def clean_currency_strings(cls, data: dict[str, Any]) -> dict[str, Any]:
        """
        Strip currency symbols and commas from numeric fields before Pydantic validates.
        Claude often returns "₹45,000.00" instead of 45000.0.
        This validator handles that at model level — not scattered in calling code.
        """
        numeric_fields = ("subtotal", "tax_amount", "discount_amount", "total_amount")
        for field in numeric_fields:
            val = data.get(field)
            if isinstance(val, str):
                # Remove ₹, $, £, commas, spaces
                cleaned = re.sub(r"[₹$£€,\s]", "", val)
                try:
                    data[field] = float(cleaned)
                except (ValueError, TypeError):
                    data[field] = None
        return data


# --------------------------------------------------------------------------- #
# Prompt                                                                         #
# --------------------------------------------------------------------------- #

def _build_extraction_prompt(raw_text: str, document_type: str = "invoice") -> str:
    # Truncate BEFORE embedding in prompt — prevents token limit blowouts
    from parser.extractor import truncate_text, MAX_TEXT_CHARS
    safe_text = truncate_text(raw_text, max_chars=MAX_TEXT_CHARS)
    return f"""You are a financial document extraction system. Extract structured data from this {document_type}.

Return ONLY a valid JSON object. No preamble. No explanation. No markdown code fences. Just the JSON.

Required JSON structure:
{{
  "invoice_number": "string or null",
  "invoice_date": "string or null",
  "due_date": "string or null",
  "currency": "INR",
  "vendor_name": "string or null",
  "vendor_address": "string or null",
  "vendor_gstin": "string or null (15-char GST number if present)",
  "buyer_name": "string or null",
  "buyer_address": "string or null",
  "buyer_gstin": "string or null",
  "subtotal": number or null,
  "tax_amount": number or null (total of all GST/VAT/tax)",
  "discount_amount": number or null,
  "total_amount": number or null,
  "line_items": [
    {{"description": "string", "quantity": number or null, "unit_price": number or null, "amount": number or null}}
  ],
  "confidence_score": integer 0-100
}}

Rules:
- All monetary values must be plain numbers (no currency symbols, no commas)
- If a field is not present in the document, use null
- confidence_score: 90-100 = all fields clear, 60-89 = some ambiguity, 0-59 = significant uncertainty
- For Indian documents: look for GSTIN, CGST, SGST, IGST fields

Document text:
{safe_text}"""


# --------------------------------------------------------------------------- #
# Extraction function                                                            #
# --------------------------------------------------------------------------- #

async def extract_invoice(
    raw_text: str,
    document_type: str = "invoice",
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[InvoiceFields | None, str, int]:
    """
    Call Claude API to extract structured invoice fields from raw text.

    Returns:
        (InvoiceFields, model_used, tokens_used) on success
        (None, model_used, tokens_used) on failure — caller handles gracefully

    WHY return tuple not raise?
    Same principle as qualifier.py — extraction failure is not exceptional.
    A failed extraction sets document.status = "failed" and stores the error.
    The pipeline continues for other documents.
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — extraction impossible")
        return None, CLAUDE_MODEL, 0

    if not raw_text.strip():
        logger.warning("Empty text passed to extract_invoice — nothing to extract")
        return None, CLAUDE_MODEL, 0

    prompt = _build_extraction_prompt(raw_text, document_type)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)

    try:
        response = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        response.raise_for_status()

        data = response.json()
        raw_response = data["content"][0]["text"].strip()
        tokens_used = data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0)

        # Parse JSON — Claude should return clean JSON but handle edge cases
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            # Try extracting JSON block if Claude added any wrapper text
            match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
            else:
                logger.error("Claude returned non-JSON: %s", raw_response[:200])
                return None, CLAUDE_MODEL, tokens_used

        # Validate with Pydantic — catches wrong types and cleans currency strings
        fields = InvoiceFields.model_validate(parsed)

        logger.info(
            "Extracted invoice: vendor=%r total=%s confidence=%d%% tokens=%d",
            fields.vendor_name, fields.total_amount, fields.confidence_score, tokens_used,
        )
        return fields, CLAUDE_MODEL, tokens_used

    except httpx.HTTPStatusError as exc:
        logger.error("Claude API HTTP error: %s", exc)
        return None, CLAUDE_MODEL, 0

    except Exception as exc:
        logger.error("Extraction failed: %s", exc)
        return None, CLAUDE_MODEL, 0

    finally:
        if own_client and client:
            await client.aclose()


# --------------------------------------------------------------------------- #
# Public adapter layer                                                           #
# Tests, pipeline.py, and routes all use these — not extract_invoice() directly #
# --------------------------------------------------------------------------- #

from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass
class ExtractionOutput:
    """
    Unified result object for the extraction pipeline.
    Wraps (InvoiceFields | None) into a consistent success/error interface.
    Same pattern as System 1's qualify_lead() → error dict approach.
    """
    success: bool
    data: dict[str, Any]           # flat dict of extracted fields (empty on failure)
    confidence: float | None       # 0.0–1.0 (converted from InvoiceFields.confidence_score)
    error: str | None = None       # set on failure
    model_used: str = CLAUDE_MODEL
    tokens_used: int = 0


def build_prompt(text: str, document_type: str = "invoice") -> str:
    """Public alias for the internal prompt builder. Used in tests."""
    return _build_extraction_prompt(text, document_type)


def parse_llm_response(raw_text: str) -> ExtractionOutput:
    """
    Parse a raw Claude API text response into ExtractionOutput.
    Handles: clean JSON, JSON wrapped in markdown fences, partial JSON.
    Returns ExtractionOutput with success=False if parsing fails entirely.
    Used in tests to validate our parsing logic independently of the HTTP call.
    """
    raw = raw_text.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Try full parse
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting the first JSON object
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return ExtractionOutput(success=False, data={}, confidence=None, error=f"JSON parse failed: {raw[:100]!r}")
        else:
            return ExtractionOutput(success=False, data={}, confidence=None, error=f"No JSON found in response: {raw[:100]!r}")

    # Validate with Pydantic
    try:
        fields = InvoiceFields.model_validate(parsed)
    except Exception as exc:
        return ExtractionOutput(success=False, data={}, confidence=None, error=f"Pydantic validation: {exc}")

    confidence = fields.confidence_score / 100.0 if fields.confidence_score else None
    return ExtractionOutput(
        success=True,
        data=fields.model_dump(exclude_none=True),
        confidence=confidence,
    )


async def extract_fields(
    text: str,
    document_type: str = "invoice",
) -> ExtractionOutput:
    """
    Public pipeline function. Calls Claude, returns ExtractionOutput.
    Never raises — errors are captured in ExtractionOutput.error.

    Used by:
      extractor/pipeline.py → process_document()
      tests/test_llm.py     → AsyncMock patches httpx.AsyncClient
    """
    if not ANTHROPIC_API_KEY:
        return ExtractionOutput(
            success=False, data={}, confidence=None,
            error="anthropic_api_key not set",
        )

    fields, model, tokens = await extract_invoice(text, document_type)

    if fields is None:
        return ExtractionOutput(
            success=False, data={}, confidence=None,
            model_used=model, tokens_used=tokens,
            error="extraction returned None — check logs for detail",
        )

    confidence = fields.confidence_score / 100.0 if fields.confidence_score else None
    return ExtractionOutput(
        success=True,
        data=fields.model_dump(exclude_none=True),
        confidence=confidence,
        model_used=model,
        tokens_used=tokens,
    )
