"""
processor/llm.py — AI extraction via Google Gemini Flash (free tier).

ARCHITECTURE CHANGE:
  OLD: Every PDF → Claude Sonnet → pay per token
  NEW: Rules extract first → AI only for MISSING fields → free Gemini Flash

WHY Gemini Flash free tier?
  - 15 RPM, 1 million tokens/day free
  - gemini-1.5-flash: fast (under 3 sec), free
  - Get key: aistudio.google.com → Get API Key (2 minutes)

WHY only send MISSING fields to AI?
  Rules got invoice_number, date, total already?
  Only ask AI about vendor_name and line_items.
  200-token prompt vs 2000-token full-document = 10x cheaper, 3x faster.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("docai.processor.llm")

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("EXTRACTION_MODEL", "gemini-1.5-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)


class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class InvoiceFields(BaseModel):
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str = "INR"
    vendor_name: str | None = None
    vendor_address: str | None = None
    vendor_gstin: str | None = None
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
        for f in ("subtotal", "tax_amount", "discount_amount", "total_amount"):
            val = data.get(f)
            if isinstance(val, str):
                cleaned = re.sub(r"[₹$£€,\s]", "", val)
                try:
                    data[f] = float(cleaned)
                except (ValueError, TypeError):
                    data[f] = None
        return data


@dataclass
class ExtractionOutput:
    success: bool
    data: dict[str, Any]
    confidence: float | None
    error: str | None = None
    model_used: str = GEMINI_MODEL
    tokens_used: int = 0


def build_prompt(
    text: str,
    document_type: str = "invoice",
    missing_fields: list[str] | None = None,
) -> str:
    from parser.extractor import truncate_text

    safe_text = truncate_text(text, max_chars=6000)

    if missing_fields:
        fields_instruction = (
            f"Extract ONLY these fields (rules-based parsing already got the rest): "
            f"{', '.join(missing_fields)}. Return null for any you cannot find."
        )
    else:
        fields_instruction = "Extract all available fields."

    return f"""Extract structured data from this {document_type}.

{fields_instruction}

Return ONLY valid JSON. No markdown. No explanation. Just JSON.

{{
  "invoice_number": null, "invoice_date": null, "due_date": null,
  "currency": "INR", "vendor_name": null, "vendor_address": null,
  "vendor_gstin": null, "buyer_name": null, "buyer_address": null,
  "buyer_gstin": null, "subtotal": null, "tax_amount": null,
  "discount_amount": null, "total_amount": null,
  "line_items": [], "confidence_score": 0
}}

Rules: monetary values = plain numbers (no ₹, no commas). Indian GSTIN = 15-char alphanumeric.
confidence_score: 90+ clear, 60-89 some ambiguity, below 60 uncertain.

Document:
{safe_text}"""


def parse_llm_response(raw_text: str) -> ExtractionOutput:
    raw = raw_text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return ExtractionOutput(
                    success=False,
                    data={},
                    confidence=None,
                    error=f"JSON parse failed: {raw[:100]!r}",
                )
        else:
            return ExtractionOutput(
                success=False, data={}, confidence=None, error=f"No JSON in response: {raw[:100]!r}"
            )

    try:
        fields = InvoiceFields.model_validate(parsed)
    except Exception as exc:
        return ExtractionOutput(success=False, data={}, confidence=None, error=str(exc))

    confidence = fields.confidence_score / 100.0 if fields.confidence_score else None
    return ExtractionOutput(
        success=True, data=fields.model_dump(exclude_none=True), confidence=confidence
    )


async def _call_gemini(prompt: str, client: httpx.AsyncClient | None = None) -> tuple[str, int]:
    if not GEMINI_API_KEY:
        return "", 0

    url = GEMINI_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        return raw, tokens
    except httpx.HTTPStatusError as exc:
        logger.error("Gemini HTTP error: %d", exc.response.status_code)
        return "", 0
    except Exception as exc:
        logger.error("Gemini call failed: %s", exc)
        return "", 0
    finally:
        if own_client and client:
            await client.aclose()


async def extract_fields(
    text: str,
    document_type: str = "invoice",
    missing_fields: list[str] | None = None,
) -> ExtractionOutput:
    """
    Call Gemini to fill in fields that rules.py couldn't extract.
    missing_fields: list of field names to ask about — keeps prompt small.
    Never raises.
    """
    if not GEMINI_API_KEY:
        return ExtractionOutput(
            success=False,
            data={},
            confidence=None,
            error="GEMINI_API_KEY not set. Get free key at aistudio.google.com",
        )

    if not text.strip():
        return ExtractionOutput(success=False, data={}, confidence=None, error="Empty text")

    if missing_fields:
        logger.info("AI filling %d missing fields: %s", len(missing_fields), missing_fields)

    prompt = build_prompt(text, document_type, missing_fields)
    raw, tokens = await _call_gemini(prompt)

    if not raw:
        return ExtractionOutput(
            success=False,
            data={},
            confidence=None,
            model_used=GEMINI_MODEL,
            tokens_used=tokens,
            error="Gemini returned empty response",
        )

    output = parse_llm_response(raw)
    output.model_used = GEMINI_MODEL
    output.tokens_used = tokens
    return output
